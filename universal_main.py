#!/usr/bin/env python3
"""Extended entry point.

Keeps the existing main.py handlers untouched and adds a fallback handler for
HTTP/HTTPS links that are not recognized by the existing Instagram/X/YouTube
router. The fallback delegates extraction to gallery-dl and yt-dlp.
"""

import logging
from pathlib import Path

import telebot
from telebot import types

import main as existing
from universal_downloader import download_public_url, extract_http_url

logger = logging.getLogger("universal_bot")

# Reuse the exact bot instance/config from the existing implementation.
bot = existing.bot


def _is_new_url_message(message: types.Message) -> bool:
    text = getattr(message, "text", None)
    if not text:
        return False
    url = extract_http_url(text)
    if not url:
        return False
    # Existing handlers must retain priority for Instagram, X/Twitter and the
    # YouTube URL forms they already recognize.
    return existing.extract_url(text) is None


def _send_files(chat_id: int, files: list[Path], reply_to: int | None = None) -> None:
    for f in files:
        try:
            with open(f, "rb") as fh:
                if existing.is_video(f):
                    bot.send_video(chat_id, fh, reply_to_message_id=reply_to)
                elif f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    bot.send_photo(chat_id, fh, reply_to_message_id=reply_to)
                else:
                    bot.send_document(chat_id, fh, reply_to_message_id=reply_to)
        except Exception:
            logger.exception("Failed sending %s", f)


def _process_fallback(chat_id: int, url: str, original_msg_id: int | None,
                      status_msg_id: int | None, is_channel: bool) -> None:
    temp_dir = None
    try:
        files, temp_dir = download_public_url(url)
        if not files:
            if not is_channel and status_msg_id:
                bot.edit_message_text(
                    "❌ இந்த link-ஐ download செய்ய முடியவில்லை. Public media/extractor support தேவை.",
                    chat_id,
                    status_msg_id,
                )
            return

        _send_files(
            chat_id,
            files,
            reply_to=None if is_channel else original_msg_id,
        )

        if is_channel and original_msg_id:
            try:
                bot.delete_message(chat_id, original_msg_id)
            except Exception:
                pass
        elif status_msg_id:
            try:
                bot.delete_message(chat_id, status_msg_id)
            except Exception:
                pass
    except Exception:
        logger.exception("Universal fallback failed for %s", url)
        if not is_channel and status_msg_id:
            try:
                bot.edit_message_text("❌ Error occurred while processing this link.", chat_id, status_msg_id)
            except Exception:
                pass
    finally:
        if temp_dir:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


@bot.message_handler(func=_is_new_url_message)
def universal_private_handler(message: types.Message):
    url = extract_http_url(message.text)
    if not url:
        return
    status = bot.reply_to(message, "⏳ Downloading media...")
    _process_fallback(message.chat.id, url, message.message_id, status.message_id, False)


@bot.channel_post_handler(func=_is_new_url_message)
def universal_channel_handler(message: types.Message):
    url = extract_http_url(message.text)
    if not url:
        return
    _process_fallback(message.chat.id, url, message.message_id, None, True)


if __name__ == "__main__":
    logger.info("Starting Ultimate Downloader Bot with universal fallback...")
    Path(existing.DOWNLOAD_ROOT).mkdir(exist_ok=True)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
