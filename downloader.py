import os
import re
import json
import uuid
import time
import shutil
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yt_dlp

from config import Config

logger = logging.getLogger(__name__)

# Simple URL patterns for quick platform detection
INSTAGRAM_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+", re.I)
TWITTER_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+", re.I)
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]+",
    re.I,
)

def is_supported_url(url: str) -> bool:
    return bool(INSTAGRAM_RE.search(url) or TWITTER_RE.search(url) or YOUTUBE_RE.search(url))

def extract_url(text: str) -> Optional[str]:
    """Return the first supported URL found in text, or None."""
    for pattern in (INSTAGRAM_RE, TWITTER_RE, YOUTUBE_RE):
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None

class MediaDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })

    def _create_temp_dir(self) -> Path:
        ts = int(time.time())
        unique = uuid.uuid4().hex[:8]
        path = Path(Config.DOWNLOAD_ROOT) / f"dl_{ts}_{unique}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup(self, path: Path) -> None:
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            logger.warning("Cleanup failed for %s: %s", path, e)

    # ── Layer 1: Cobalt API ──────────────────────────────────────────────────
    def _cobalt_download(self, url: str, dest: Path) -> List[Path]:
        files: List[Path] = []
        payload = {
            "url": url,
            "vCodec": "h264",
            "vQuality": "1080",
            "aFormat": "mp3",
            "filenameStyle": "basic",
            "isAudioOnly": False,
            "disableMetadata": True,
        }

        for endpoint in Config.COBALT_ENDPOINTS:
            try:
                # Newer Cobalt uses POST / with Accept: application/json
                r = self.session.post(
                    endpoint.rstrip("/") + "/",
                    json=payload,
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                if r.status_code != 200:
                    # Fallback older style
                    r = self.session.post(
                        endpoint.rstrip("/") + "/api/json",
                        json=payload,
                        timeout=30,
                    )
                data = r.json()

                status = data.get("status")
                if status in ("error", "rate-limit"):
                    logger.debug("Cobalt error on %s: %s", endpoint, data.get("text"))
                    continue

                # Single file
                if status == "redirect" or "url" in data:
                    media_url = data.get("url") or data.get("redirect")
                    if media_url:
                        fpath = self._save_url(media_url, dest)
                        if fpath:
                            files.append(fpath)
                            return files

                # Picker / multi
                if status == "picker" and "picker" in data:
                    for item in data["picker"]:
                        media_url = item.get("url")
                        if media_url:
                            fpath = self._save_url(media_url, dest)
                            if fpath:
                                files.append(fpath)
                    if files:
                        return files

            except Exception as e:
                logger.debug("Cobalt endpoint %s failed: %s", endpoint, e)
                continue
        return files

    def _save_url(self, media_url: str, dest: Path) -> Optional[Path]:
        try:
            r = self.session.get(media_url, stream=True, timeout=60)
            r.raise_for_status()
            # Guess extension
            ctype = r.headers.get("content-type", "")
            ext = ".mp4" if "video" in ctype else ".jpg" if "image" in ctype else ".bin"
            name = f"{uuid.uuid4().hex[:10]}{ext}"
            fpath = dest / name
            with open(fpath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return fpath if fpath.stat().st_size > 0 else None
        except Exception as e:
            logger.warning("Failed to save media URL: %s", e)
            return None

    # ── Layer 2: gallery-dl ──────────────────────────────────────────────────
    def _gallery_dl(self, url: str, dest: Path) -> List[Path]:
        cmd = [
            "gallery-dl",
            "--dest", str(dest),
            "--filename", "{id}_{num}.{extension}",
            "--no-mtime",
            "--quiet",
            url,
        ]
        # Extra flags helpful for X/Twitter sensitive content
        if TWITTER_RE.search(url):
            cmd.extend(["--cookies-from-browser", "chrome"])  # optional; remove if not desired

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=Config.TOOL_TIMEOUT,
                check=False,
            )
            if result.returncode != 0:
                logger.debug("gallery-dl stderr: %s", result.stderr[:500])
                return []

            files = sorted(
                [p for p in dest.rglob("*") if p.is_file() and p.stat().st_size > 0],
                key=lambda p: p.name,
            )
            return files
        except subprocess.TimeoutExpired:
            logger.warning("gallery-dl timed out for %s", url)
            return []
        except FileNotFoundError:
            logger.error("gallery-dl binary not found in PATH")
            return []
        except Exception as e:
            logger.warning("gallery-dl failed: %s", e)
            return []

    # ── Layer 3: yt-dlp ──────────────────────────────────────────────────────
    def _ytdlp(self, url: str, dest: Path) -> List[Path]:
        ydl_opts = {
            "outtmpl": str(dest / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "ignoreerrors": True,
            "retries": 3,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return []
            files = sorted(
                [p for p in dest.rglob("*") if p.is_file() and p.stat().st_size > 0],
                key=lambda p: p.name,
            )
            return files
        except yt_dlp.utils.DownloadError as e:
            if "No video formats found" in str(e) or "Unsupported URL" in str(e):
                logger.debug("yt-dlp format issue: %s", e)
            else:
                logger.warning("yt-dlp DownloadError: %s", e)
            return []
        except Exception as e:
            logger.warning("yt-dlp failed: %s", e)
            return []

    # ── Public entry point ───────────────────────────────────────────────────
    def download(self, url: str) -> Tuple[List[Path], Path]:
        """
        Returns (list_of_media_files, temp_dir).
        Caller MUST call cleanup on the temp_dir when finished.
        """
        dest = self._create_temp_dir()
        files: List[Path] = []

        try:
            # Layer 1 – Cobalt (fastest for most public posts)
            files = self._cobalt_download(url, dest)
            if files:
                logger.info("Cobalt succeeded for %s (%d files)", url, len(files))
                return files, dest

            # Layer 2 – gallery-dl (Instagram carousels + X sensitive)
            files = self._gallery_dl(url, dest)
            if files:
                logger.info("gallery-dl succeeded for %s (%d files)", url, len(files))
                return files, dest

            # Layer 3 – yt-dlp (YouTube / fallback video streams)
            files = self._ytdlp(url, dest)
            if files:
                logger.info("yt-dlp succeeded for %s (%d files)", url, len(files))
                return files, dest

            logger.warning("All extractors failed for %s", url)
            return [], dest

        except Exception as e:
            logger.exception("Unexpected error downloading %s: %s", url, e)
            return [], dest
