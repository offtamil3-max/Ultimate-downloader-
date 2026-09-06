"""Media download engine with diagnostics and a local command-line test."""
import argparse
import os
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Tuple

import requests
import yt_dlp

from config import Config

logger = logging.getLogger(__name__)

import re
INSTAGRAM_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+", re.I)
TWITTER_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+", re.I)
YOUTUBE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]+", re.I)

def extract_url(text: str):
    for pattern in (INSTAGRAM_RE, TWITTER_RE, YOUTUBE_RE):
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


class MediaDownloader:
    def __init__(self) -> None:
        # Android/Termux does not always export this loader path when launched by a service.
        # Without it, ffmpeg can be present but fails to load libplacebo/libc++.
        termux_lib = Path(os.sys.prefix) / "lib"
        existing_loader_path = os.environ.get("LD_LIBRARY_PATH", "")
        if termux_lib.is_dir() and str(termux_lib) not in existing_loader_path.split(":"):
            os.environ["LD_LIBRARY_PATH"] = f"{termux_lib}:{existing_loader_path}".rstrip(":")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "UltimateDownloader/1.0", "Accept": "application/json"})
        self.diagnostics: List[str] = []

    @property
    def last_error(self) -> str:
        return self.diagnostics[-1] if self.diagnostics else "No extractor returned media."

    def _record(self, source: str, detail: object) -> None:
        message = f"{source}: {detail}"
        self.diagnostics.append(message)
        logger.warning(message)

    def _create_temp_dir(self) -> Path:
        directory = Path(Config.DOWNLOAD_ROOT) / f"dl_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    def cleanup(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    # Compatibility with the old main.py call site.
    _cleanup = cleanup

    @staticmethod
    def _files(dest: Path) -> List[Path]:
        return sorted((p for p in dest.rglob("*") if p.is_file() and p.stat().st_size > 0), key=lambda p: p.name)

    def _ytdlp(self, url: str, dest: Path) -> List[Path]:
        if not shutil.which("ffmpeg"):
            self._record("yt-dlp prerequisite", "ffmpeg is missing from PATH; video and audio cannot be merged")
        options = {
            "outtmpl": str(dest / "%(id)s.%(ext)s"),
            "format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": Config.TOOL_TIMEOUT,
            "quiet": True,
            "no_warnings": False,
            "logger": logger,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                self._record("yt-dlp", "extract_info returned no metadata")
                return []
        except Exception as exc:
            self._record("yt-dlp", f"{type(exc).__name__}: {exc}")
            return []
        files = self._files(dest)
        if not files:
            self._record("yt-dlp", "completed without creating an output file")
        return files

    def _cobalt_download(self, url: str, dest: Path) -> List[Path]:
        payload = {"url": url, "vCodec": "h264", "vQuality": "1080", "filenameStyle": "basic", "isAudioOnly": False}
        for endpoint in Config.COBALT_ENDPOINTS:
            try:
                response = self.session.post(endpoint, json=payload, timeout=Config.COBALT_TIMEOUT)
                if response.status_code != 200:
                    self._record("Cobalt", f"{endpoint} HTTP {response.status_code}: {response.text[:500]}")
                    continue
                data = response.json()
                if data.get("status") in {"error", "rate-limit"}:
                    self._record("Cobalt", f"{endpoint} {data.get('status')}: {data.get('text', data)}")
                    continue
                urls = [x for x in (data.get("url"), data.get("redirect")) if x]
                urls += [item["url"] for item in data.get("picker", []) if item.get("url")]
                for media_url in urls:
                    output = dest / f"cobalt_{uuid.uuid4().hex[:10]}.mp4"
                    with self.session.get(media_url, stream=True, timeout=Config.MEDIA_TIMEOUT) as media:
                        media.raise_for_status()
                        with output.open("wb") as handle:
                            for chunk in media.iter_content(1024 * 1024):
                                if chunk:
                                    handle.write(chunk)
                    if output.stat().st_size:
                        return [output]
                self._record("Cobalt", f"{endpoint} returned no downloadable URL: {data}")
            except Exception as exc:
                self._record("Cobalt", f"{endpoint}: {type(exc).__name__}: {exc}")
        return []

    def _gallery_dl(self, url: str, dest: Path) -> List[Path]:
        try:
            result = subprocess.run(["gallery-dl", "--dest", str(dest), "--filename", "{id}_{num}.{extension}", "--no-mtime", url], capture_output=True, text=True, timeout=Config.TOOL_TIMEOUT, check=False)
        except FileNotFoundError:
            self._record("gallery-dl", "executable is not installed or is not on PATH")
            return []
        except subprocess.TimeoutExpired:
            self._record("gallery-dl", f"timed out after {Config.TOOL_TIMEOUT}s")
            return []
        if result.returncode:
            self._record("gallery-dl", f"exit {result.returncode}: {(result.stderr or result.stdout).strip()[:1000]}")
            return []
        files = self._files(dest)
        if not files:
            self._record("gallery-dl", "completed without creating an output file")
        return files

    def download(self, url: str) -> Tuple[List[Path], Path]:
        self.diagnostics = []
        dest = self._create_temp_dir()
        # Public Cobalt can be rate-limited; yt-dlp is the correct first extractor for YouTube.
        extractors = (self._ytdlp, self._cobalt_download) if "youtu" in url.lower() else (self._gallery_dl, self._cobalt_download, self._ytdlp)
        for extractor in extractors:
            files = extractor(url, dest)
            if files:
                return files, dest
        return [], dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Test the downloader without Telegram")
    parser.add_argument("url")
    parser.add_argument("--keep", action="store_true", help="retain the output directory for inspection")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    downloader = MediaDownloader()
    files, directory = downloader.download(args.url)
    print(f"output directory: {directory.resolve()}")
    for file in files:
        stat = file.stat()
        print(f"file: {file.name} bytes={stat.st_size} mode={stat.st_mode & 0o777:o}")
    if not files:
        print("failure diagnostics:")
        print("\n".join(downloader.diagnostics))
    if not args.keep:
        downloader.cleanup(directory)
    return 0 if files else 1


if __name__ == "__main__":
    raise SystemExit(main())
