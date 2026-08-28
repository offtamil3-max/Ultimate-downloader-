#!/usr/bin/env python3
"""
Production Telegram media-downloader bot.
Handles Instagram / X / YouTube links in private chats and channels.
"""

import logging
import os
from pathlib import Path
from typing import List

import telebot
from telebot import types
from telebot.types import InputMediaPhoto, InputMediaVideo

from config import Config
from downloader import MediaDownloader, extract_url, is_supported_url

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

Config.validate()
bot = telebot.TeleBot(Config.BOT_TOKEN, parse_mode=None)
downloader = MediaDownloader()

# ── Helpers ─────────────────────────────────────────────────────────────────
def is_video(path: Path) -> bool:
    return path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}

def send_media_group_safe(chat_id: int, files: List[Path], reply_to: int | None = None) -> None:
    """Send files in batches of Config.MAX_MEDIA_GROUP using media groups."""
    for i in range(0, len(files), Config.MAX_MEDIA_GROUP):
        batch = files[i : i + Config.MAX_MEDIA_GROUP]
        media = []
        open_files = []  # keep handles open until send completes

        try:
            for f in batch:
                fh = open(f, "rb")
                open_files.append(fh)
                if is_video(f):
                    media.append(InputMediaVideo(fh))
                else:
                    media.append(InputMediaPhoto(fh))

            bot.send_media_group(
                chat_id,
                media,
                reply_to_message_id=reply_to,
                allow_sending_without_reply=True,
            )
        finally:
            for fh in open_files:
                try:
                    fh.close()
                except Exception:
                    pass

def process_media(chat_id: int, url: str, original_msg_id: int | None, status_msg_id: int | None, is_channel: bool) -> None:
    """Core download + send + cleanup logic."""
    files: List[Path] = []
    temp_dir: Path | None = None

    try:
        files, temp_dir = downloader.download(url)
        if not files:
            if not is_channel and status_msg_id:
                bot.edit_message_text(
                    "❌ Could not download media from this link.",
                    chat_id,
                    status_msg_id,
                )
            return

        # Send media
        if len(files) == 1:
            f = files[0]
            with open(f, "rb") as fh:
                if is_video(f):
                    bot.send_video(chat_id, fh, reply_to_message_id=None if is_channel else original_msg_id)
                else:
                    bot.send_photo(chat_id, fh, reply_to_message_id=None if is_channel else original_msg_id)
        else:
            send_media_group_safe(chat_id, files, reply_to=None if is_channel else original_msg_id)

        # Channel behaviour: delete the original link message
        if is_channel and original_msg_id:
            try:
                bot.delete_message(chat_id, original_msg_id)
            except Exception as e:
                logger.warning("Failed to delete channel message %s: %s", original_msg_id, e)

        # Private chat: remove status message
        if not is_channel and status_msg_id:
            try:
                bot.delete_message(chat_id, status_msg_id)
            except Exception:
                pass

    except Exception as e:
        logger.exception("process_media failed: %s", e)
        if not is_channel and status_msg_id:
            try:
                bot.edit_message_text("❌ An error occurred while processing the media.", chat_id, status_msg_id)
            except Exception:
                pass
    finally:
        if temp_dir:
            downloader._cleanup(temp_dir)

# ── Handlers ───────────────────────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text and extract_url(m.text) is not None)
def private_handler(message: types.Message):
    """Private chat / group: show status, download, reply, clean status."""
    if message.chat.type not in ("private", "group", "supergroup"):
        return
    # In supergroups we still treat as “private-like” unless it is a channel_post
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
    """Channel / linked discussion: silent download + replace original message."""
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

# ── Startup ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting media-downloader bot…")
    # Ensure download root exists
    Path(Config.DOWNLOAD_ROOT).mkdir(exist_ok=True)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
