#!/usr/bin/env python3
"""Telegram interface for Ultimate Downloader."""
import logging
from pathlib import Path
from typing import List, Optional

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from telebot.types import InputMediaPhoto, InputMediaVideo

from config import Config
from downloader import MediaDownloader, extract_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
Config.validate()
bot = telebot.TeleBot(Config.BOT_TOKEN, parse_mode=None)
downloader = MediaDownloader()


def is_video(path: Path) -> bool:
    return path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}


def too_large(files: List[Path]) -> Optional[str]:
    oversized = [f"{path.name} ({path.stat().st_size / 1024 / 1024:.1f} MB)" for path in files if path.stat().st_size > Config.MAX_UPLOAD_BYTES]
    if oversized:
        return f"Downloaded media exceeds this bot's {Config.MAX_UPLOAD_BYTES // 1024 // 1024} MB upload limit: {', '.join(oversized)}."
    return None


def send_media_group_safe(chat_id: int, files: List[Path], reply_to: int | None = None) -> None:
    for start in range(0, len(files), Config.MAX_MEDIA_GROUP):
        handles = []
        try:
            media = []
            for file in files[start:start + Config.MAX_MEDIA_GROUP]:
                handle = file.open("rb")
                handles.append(handle)
                media.append(InputMediaVideo(handle) if is_video(file) else InputMediaPhoto(handle))
            bot.send_media_group(chat_id, media, reply_to_message_id=reply_to, allow_sending_without_reply=True)
        finally:
            for handle in handles:
                handle.close()


def process_media(chat_id: int, url: str, original_msg_id: int | None, status_msg_id: int | None, is_channel: bool) -> None:
    directory = None
    try:
        files, directory = downloader.download(url)
        if not files:
            raise RuntimeError("; ".join(downloader.diagnostics[-2:]) or "All download methods failed.")
        size_error = too_large(files)
        if size_error:
            raise RuntimeError(size_error)
        if len(files) == 1:
            with files[0].open("rb") as handle:
                sender = bot.send_video if is_video(files[0]) else bot.send_photo
                sender(chat_id, handle, reply_to_message_id=None if is_channel else original_msg_id)
        else:
            send_media_group_safe(chat_id, files, None if is_channel else original_msg_id)
        if not is_channel and status_msg_id:
            bot.delete_message(chat_id, status_msg_id)
    except ApiTelegramException as exc:
        logger.exception("Telegram upload failed")
        if not is_channel and status_msg_id:
            bot.edit_message_text(f"❌ Downloaded the media, but Telegram rejected the upload: {exc.description[:300]}", chat_id, status_msg_id)
    except Exception as exc:
        logger.exception("Media processing failed")
        if not is_channel and status_msg_id:
            bot.edit_message_text(f"❌ Download failed: {str(exc)[:700]}", chat_id, status_msg_id)
    finally:
        if directory:
            downloader.cleanup(directory)


@bot.message_handler(commands=["start"])
def start_handler(message: types.Message) -> None:
    bot.reply_to(message, "Send a supported Instagram, X, or YouTube link.")


@bot.message_handler(func=lambda message: message.text and extract_url(message.text) is not None)
def private_handler(message: types.Message) -> None:
    status = bot.reply_to(message, "⏳ Downloading media...")
    process_media(message.chat.id, extract_url(message.text), message.message_id, status.message_id, False)


@bot.channel_post_handler(func=lambda message: message.text and extract_url(message.text) is not None)
def channel_handler(message: types.Message) -> None:
    process_media(message.chat.id, extract_url(message.text), message.message_id, None, True)


if __name__ == "__main__":
    Path(Config.DOWNLOAD_ROOT).mkdir(exist_ok=True)
    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
