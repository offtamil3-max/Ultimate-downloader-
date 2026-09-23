#!/usr/bin/env python3
"""Railway entry point for the Telegram-only Ultimate Downloader bot.

Instagram DM/webhook forwarding has been intentionally removed.
The bot now accepts a normal Instagram/X/YouTube URL pasted into Telegram
and downloads the media through the existing downloader stack.
"""

import logging
import os
import threading
from pathlib import Path

from flask import Flask, jsonify, request
from telebot import types

import main as existing
from universal_downloader import download_public_url, extract_http_url

logger = logging.getLogger("universal_bot")

bot = existing.bot
PORT = int(os.getenv("PORT", "8080"))
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://ultimate-downloader-production.up.railway.app",
).strip().rstrip("/")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

app = Flask(__name__)


def _is_new_url_message(message: types.Message) -> bool:
    text = getattr(message, "text", None)
    if not text:
        return False
    url = extract_http_url(text)
    if not url:
        return False
    # Existing Instagram/X/YouTube handlers keep priority.
    return existing.extract_url(text) is None


def _send_files(
    chat_id: int,
    files: list[Path],
    reply_to: int | None = None,
) -> None:
    for f in files:
        try:
            with open(f, "rb") as fh:
                if existing.is_video(f):
                    bot.send_video(
                        chat_id,
                        fh,
                        reply_to_message_id=reply_to,
                        allow_sending_without_reply=True,
                    )
                elif f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    bot.send_photo(
                        chat_id,
                        fh,
                        reply_to_message_id=reply_to,
                        allow_sending_without_reply=True,
                    )
                else:
                    bot.send_document(
                        chat_id,
                        fh,
                        reply_to_message_id=reply_to,
                        allow_sending_without_reply=True,
                    )
        except Exception:
            logger.exception("Failed sending %s", f)


def _process_fallback(
    chat_id: int,
    url: str,
    original_msg_id: int | None,
    status_msg_id: int | None,
    is_channel: bool,
) -> None:
    temp_dir = None
    try:
        files, temp_dir = download_public_url(url)
        if not files:
            if not is_channel and status_msg_id:
                bot.edit_message_text(
                    "❌ இந்த link-ஐ download செய்ய முடியவில்லை.",
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
                bot.edit_message_text(
                    "❌ Error occurred while processing this link.",
                    chat_id,
                    status_msg_id,
                )
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
    _process_fallback(
        message.chat.id,
        url,
        message.message_id,
        status.message_id,
        False,
    )


@bot.channel_post_handler(func=_is_new_url_message)
def universal_channel_handler(message: types.Message):
    url = extract_http_url(message.text)
    if not url:
        return
    _process_fallback(
        message.chat.id,
        url,
        message.message_id,
        None,
        True,
    )


@app.post("/telegram/webhook")
def telegram_webhook():
    if TELEGRAM_WEBHOOK_SECRET:
        provided = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if provided != TELEGRAM_WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "Forbidden"}), 403

    try:
        raw = request.data.decode("utf-8")
        update = types.Update.de_json(raw)
        if update is None:
            return jsonify({"ok": False, "error": "invalid update"}), 400

        bot.process_new_updates([update])
        logger.info(
            "Telegram update dispatched successfully: update_id=%s",
            getattr(update, "update_id", None),
        )
        return jsonify({"ok": True}), 200
    except Exception:
        logger.exception("Telegram webhook update processing failed")
        return jsonify({"ok": False}), 500


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "ultimate-downloader"}), 200


@app.get("/privacy-policy")
def privacy_policy():
    return (
        """
        <!doctype html>
        <html><head><meta charset="utf-8">
        <title>Privacy Policy - Ultimate Downloader</title></head>
        <body>
        <h1>Privacy Policy</h1>
        <p>Ultimate Downloader processes media links sent to its Telegram bot
        to provide the requested download functionality.</p>
        <p>Downloaded media is processed temporarily and removed after
        processing, subject to hosting/platform logs outside the bot's control.</p>
        <p>We do not intentionally sell personal information.</p>
        </body></html>
        """,
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


def configure_telegram_webhook() -> None:
    url = f"{PUBLIC_BASE_URL}/telegram/webhook"
    try:
        if TELEGRAM_WEBHOOK_SECRET:
            bot.set_webhook(
                url=url,
                secret_token=TELEGRAM_WEBHOOK_SECRET,
                drop_pending_updates=False,
            )
        else:
            bot.set_webhook(
                url=url,
                drop_pending_updates=False,
            )
        logger.info("Telegram webhook configured: %s", url)
        try:
            info = bot.get_webhook_info()
            logger.info(
                "Telegram webhook info: url=%s pending=%s last_error=%s",
                getattr(info, "url", ""),
                getattr(info, "pending_update_count", None),
                getattr(info, "last_error_message", None),
            )
        except Exception:
            logger.exception("Could not read Telegram webhook info")
    except Exception:
        logger.exception("Failed to configure Telegram webhook")


if __name__ == "__main__":
    logger.info("Starting Ultimate Downloader Bot (Telegram link mode only)...")
    Path(existing.DOWNLOAD_ROOT).mkdir(exist_ok=True)
    configure_telegram_webhook()
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
        use_reloader=False,
    )
