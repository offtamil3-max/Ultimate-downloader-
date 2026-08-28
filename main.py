#!/usr/bin/env python3
"""
Ultimate Downloader Bot - Single File
Supports Instagram, X (Twitter), YouTube
"""

import os
import re
import uuid
import time
import shutil
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import requests
import yt_dlp
import telebot
from telebot import types
from telebot.types import InputMediaPhoto, InputMediaVideo

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
COOKIES_FROM_BROWSER = os.getenv("COOKIES_FROM_BROWSER", "")
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")

COBALT_ENDPOINTS = [
    "https://api.cobalt.tools/",
    "https://co.wuk.sh/api/json",
]
MAX_MEDIA_GROUP = 10
DOWNLOAD_ROOT = "downloads"
TOOL_TIMEOUT = 120

# Cookies file தானாக உருவாக்க (Railway-க்கு)
if COOKIES_CONTENT:
    cookies_path = Path("cookies.txt")
    cookies_path.write_text(COOKIES_CONTENT)
    COOKIES_FILE = str(cookies_path)

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

# ==================== URL PATTERNS ====================
INSTAGRAM_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+", re.I)
TWITTER_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+", re.I)
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]+",
    re.I,
)

def extract_url(text: str) -> Optional[str]:
    for pattern in (INSTAGRAM_RE, TWITTER_RE, YOUTUBE_RE):
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None

# ==================== DOWNLOADER ====================
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
        path = Path(DOWNLOAD_ROOT) / f"dl_{ts}_{unique}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup(self, path: Path) -> None:
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            logger.warning("Cleanup failed: %s", e)

    def _save_url(self, media_url: str, dest: Path) -> Optional[Path]:
        try:
            r = self.session.get(media_url, stream=True, timeout=60)
            r.raise_for_status()
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
            logger.warning("Failed to save media: %s", e)
            return None

    def _cobalt_download(self, url: str, dest: Path) -> List[Path]:
        files = []
        payload = {
            "url": url,
            "vCodec": "h264",
            "vQuality": "1080",
            "aFormat": "mp3",
            "filenameStyle": "basic",
            "isAudioOnly": False,
            "disableMetadata": True,
        }

        for endpoint in COBALT_ENDPOINTS:
            try:
                r = self.session.post(
                    endpoint.rstrip("/") + "/",
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=30,
                )
                if r.status_code != 200:
                    r = self.session.post(endpoint.rstrip("/") + "/api/json", json=payload, timeout=30)

                data = r.json()
                status = data.get("status")

                if status in ("error", "rate-limit"):
                    continue

                if status == "redirect" or "url" in data:
                    media_url = data.get("url") or data.get("redirect")
                    if media_url:
                        fpath = self._save_url(media_url, dest)
                        if fpath:
                            files.append(fpath)
                            return files

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
                logger.debug("Cobalt failed: %s", e)
                continue
        return files

    def _gallery_dl(self, url: str, dest: Path) -> List[Path]:
        cmd = [
            "gallery-dl",
            "--dest", str(dest),
            "--filename", "{id}_{num}.{extension}",
            "--no-mtime",
            "--quiet",
            "--option", "twitter.videos=true",
            "--option", "twitter.quoted=true",
            url,
        ]

        if COOKIES_FILE and Path(COOKIES_FILE).exists():
            cmd.extend(["--cookies", COOKIES_FILE])
        elif COOKIES_FROM_BROWSER:
            cmd.extend(["--cookies-from-browser", COOKIES_FROM_BROWSER])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=TOOL_TIMEOUT)
            if result.returncode != 0:
                logger.warning("gallery-dl error: %s", result.stderr[:600])
                return []

            files = sorted(
                [p for p in dest.rglob("*") if p.is_file() and p.stat().st_size > 0],
                key=lambda p: p.name,
            )
            return files
        except Exception as e:
            logger.warning("gallery-dl failed: %s", e)
            return []

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

        if COOKIES_FILE and Path(COOKIES_FILE).exists():
            ydl_opts["cookiefile"] = COOKIES_FILE
        elif COOKIES_FROM_BROWSER:
            ydl_opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)

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
        except Exception as e:
            logger.warning("yt-dlp failed: %s", e)
            return []

    def download(self, url: str) -> Tuple[List[Path], Path]:
        dest = self._create_temp_dir()
        files = []

        try:
            files = self._cobalt_download(url, dest)
            if files:
                logger.info("Cobalt success: %d files", len(files))
                return files, dest

            files = self._gallery_dl(url, dest)
            if files:
                logger.info("gallery-dl success: %d files", len(files))
                return files, dest

            files = self._ytdlp(url, dest)
            if files:
                logger.info("yt-dlp success: %d files", len(files))
                return files, dest

            return [], dest
        except Exception as e:
            logger.exception("Download error: %s", e)
            return [], dest

# ==================== BOT ====================
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
downloader = MediaDownloader()

def is_video(path: Path) -> bool:
    return path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}

def send_media_group_safe(chat_id: int, files: List[Path], reply_to: int | None = None) -> None:
    for i in range(0, len(files), MAX_MEDIA_GROUP):
        batch = files[i:i + MAX_MEDIA_GROUP]
        media = []
        open_files = []
        try:
            for f in batch:
                fh = open(f, "rb")
                open_files.append(fh)
                if is_video(f):
                    media.append(InputMediaVideo(fh))
                else:
                    media.append(InputMediaPhoto(fh))
            bot.send_media_group(chat_id, media, reply_to_message_id=reply_to, allow_sending_without_reply=True)
        finally:
            for fh in open_files:
                try:
                    fh.close()
                except:
                    pass

def process_media(chat_id: int, url: str, original_msg_id: int | None, status_msg_id: int | None, is_channel: bool):
    files = []
    temp_dir = None

    try:
        files, temp_dir = downloader.download(url)
        if not files:
            if not is_channel and status_msg_id:
                bot.edit_message_text("❌ Could not download media from this link.", chat_id, status_msg_id)
            return

        if len(files) == 1:
            f = files[0]
            with open(f, "rb") as fh:
                if is_video(f):
                    bot.send_video(chat_id, fh, reply_to_message_id=None if is_channel else original_msg_id)
                else:
                    bot.send_photo(chat_id, fh, reply_to_message_id=None if is_channel else original_msg_id)
        else:
            send_media_group_safe(chat_id, files, reply_to=None if is_channel else original_msg_id)

        if is_channel and original_msg_id:
            try:
                bot.delete_message(chat_id, original_msg_id)
            except:
                pass

        if not is_channel and status_msg_id:
            try:
                bot.delete_message(chat_id, status_msg_id)
            except:
                pass

    except Exception as e:
        logger.exception("process_media error: %s", e)
        if not is_channel and status_msg_id:
            try:
                bot.edit_message_text("❌ Error occurred while processing.", chat_id, status_msg_id)
            except:
                pass
    finally:
        if temp_dir:
            downloader._cleanup(temp_dir)

# ==================== HANDLERS ====================
@bot.message_handler(commands=['start'])
def start_handler(message):
    bot.reply_to(message, "Hi I am ultimate downloader bot")

@bot.message_handler(func=lambda m: m.text and extract_url(m.text) is not None)
def private_handler(message: types.Message):
    url = extract_url(message.text)
    if not url:
        return
    status = bot.reply_to(message, "⏳ Downloading media...")
    process_media(
        chat_id=message.chat.id,
        url=url,
        original_msg_id=message.message_id,
        status_msg_id=status.message_id,
        is_channel=False,
    )

@bot.channel_post_handler(func=lambda m: m.text and extract_url(m.text) is not None)
def channel_handler(message: types.Message):
    url = extract_url(message.text)
    if not url:
        return
    process_media(
        chat_id=message.chat.id,
        url=url,
        original_msg_id=message.message_id,
        status_msg_id=None,
        is_channel=True,
    )

# ==================== START ====================
if __name__ == "__main__":
    logger.info("Starting Ultimate Downloader Bot...")
    Path(DOWNLOAD_ROOT).mkdir(exist_ok=True)

    # Muk்கியம்: முன்பு webhook set பண்ணி இருந்தா, polling எந்த
    # update-உம் receive பண்ணாது (start கூட வேலை செய்யாது).
    # இதை clear பண்றது mandatory.
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception as e:
        logger.warning("remove_webhook failed: %s", e)

    me = bot.get_me()
    logger.info("Logged in as @%s (id=%s) — bot is now polling for messages...", me.username, me.id)

    bot.infinity_polling(timeout=60, long_polling_timeout=60)
