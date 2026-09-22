"""Instagram DM webhook bridge for Ultimate Downloader.

Receives Instagram Messaging API webhook events and forwards supported media
attachments/URLs to the configured Telegram channel. Secrets are read only
from environment variables.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request

from universal_downloader import download_public_url

logger = logging.getLogger("instagram_webhook")

VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "")
TARGET_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
PORT = int(os.getenv("PORT", "8080"))

app = Flask(__name__)
_seen_mids: set[str] = set()
_seen_lock = threading.Lock()


def _already_seen(mid: str | None) -> bool:
    if not mid:
        return False
    with _seen_lock:
        if mid in _seen_mids:
            return True
        _seen_mids.add(mid)
        if len(_seen_mids) > 2000:
            _seen_mids.clear()
            _seen_mids.add(mid)
    return False


def _send_to_telegram(bot, files: list[Path], caption: str | None = None) -> None:
    if not TARGET_CHANNEL_ID:
        logger.error("TELEGRAM_CHANNEL_ID is not configured")
        return

    for f in files:
        try:
            with f.open("rb") as fh:
                if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                    bot.send_video(TARGET_CHANNEL_ID, fh, caption=caption)
                elif f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    bot.send_photo(TARGET_CHANNEL_ID, fh, caption=caption)
                else:
                    bot.send_document(TARGET_CHANNEL_ID, fh, caption=caption)
        except Exception:
            logger.exception("Telegram upload failed for %s", f)


def _download_and_forward(bot, url: str, caption: str | None = None) -> None:
    temp_dir = None
    try:
        files, temp_dir = download_public_url(url)
        if not files:
            logger.warning("No media downloaded from Instagram attachment: %s", url)
            return
        _send_to_telegram(bot, files, caption=caption)
    except Exception:
        logger.exception("Instagram media processing failed")
    finally:
        if temp_dir:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


def _handle_event(bot, event: dict[str, Any]) -> None:
    messaging = event.get("messaging") or []
    for item in messaging:
        message = item.get("message") or {}
        mid = message.get("mid")
        if _already_seen(mid):
            continue

        urls: list[str] = []

        # Instagram Messaging API commonly exposes shared media as an
        # attachment payload URL. Accept any attachment URL that is present.
        for attachment in message.get("attachments") or []:
            payload = attachment.get("payload") or {}
            url = payload.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)

        # Also accept a normal URL sent in the DM text.
        text = message.get("text")
        if isinstance(text, str):
            import re
            urls.extend(re.findall(r"https?://[^\s<>\"']+", text))

        for url in dict.fromkeys(urls):
            threading.Thread(
                target=_download_and_forward,
                args=(bot, url, None),
                daemon=True,
            ).start()


@app.get("/instagram/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
        return challenge or "", 200

    return "Forbidden", 403


@app.post("/instagram/webhook")
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    bot = app.config.get("telegram_bot")
    if bot is None:
        return jsonify({"ok": False, "error": "Telegram bot not initialized"}), 503

    # Acknowledge quickly; process media in background.
    for entry in payload.get("entry") or []:
        _handle_event(bot, entry)

    return jsonify({"ok": True}), 200


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "ultimate-downloader"}), 200


def start_instagram_webhook(bot) -> None:
    if not VERIFY_TOKEN:
        logger.warning("INSTAGRAM_VERIFY_TOKEN is not configured; webhook verification will fail.")
    if not TARGET_CHANNEL_ID:
        logger.warning("TELEGRAM_CHANNEL_ID is not configured; Instagram media cannot be forwarded.")

    app.config["telegram_bot"] = bot

    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False),
        daemon=True,
    )
    thread.start()
    logger.info("Instagram webhook listening on port %s", PORT)
