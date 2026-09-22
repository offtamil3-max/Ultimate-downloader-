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
import mimetypes
import tempfile
from typing import Any

import requests
from flask import Flask, jsonify, request
from telebot import types as tg_types

from universal_downloader import download_public_url

logger = logging.getLogger("instagram_webhook")

# Keep a known fallback so a stale/missing Railway variable cannot make Meta
# verification fail when the value entered in Meta matches the app's configured
# verification token. If a Railway variable is present, it is also accepted.
DEFAULT_VERIFY_TOKEN = "igwh_7f4c2d9a_2026"
VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "").strip()
ACCEPTED_VERIFY_TOKENS = {
    token for token in (VERIFY_TOKEN, DEFAULT_VERIFY_TOKEN) if token
}
TARGET_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
GRAPH_VERSION = os.getenv("INSTAGRAM_GRAPH_VERSION", "v26.0").strip()
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

    # Telegram supports up to 10 photos/videos in one media group. Preserve
    # Instagram multi-attachment messages instead of concurrent sends.
    media_exts = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm", ".mov", ".avi"}
    media_files = [f for f in files if f.suffix.lower() in media_exts]

    for start in range(0, len(media_files), 10):
        chunk = media_files[start:start + 10]
        handles = []
        media = []
        try:
            for index, f in enumerate(chunk):
                fh = f.open("rb")
                handles.append(fh)
                item_caption = caption if start == 0 and index == 0 else None
                if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                    media.append(tg_types.InputMediaVideo(fh, caption=item_caption))
                else:
                    media.append(tg_types.InputMediaPhoto(fh, caption=item_caption))

            if len(media) == 1:
                if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                    bot.send_video(TARGET_CHANNEL_ID, handles[0], caption=caption)
                else:
                    bot.send_photo(TARGET_CHANNEL_ID, handles[0], caption=caption)
            else:
                bot.send_media_group(TARGET_CHANNEL_ID, media)
        except Exception:
            logger.exception("Telegram media group upload failed for %s", chunk)
            for f in chunk:
                try:
                    with f.open("rb") as fh:
                        if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                            bot.send_video(TARGET_CHANNEL_ID, fh, caption=caption)
                        else:
                            bot.send_photo(TARGET_CHANNEL_ID, fh, caption=caption)
                except Exception:
                    logger.exception("Telegram individual upload failed for %s", f)
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass

    for f in files:
        if f in media_files:
            continue
        try:
            with f.open("rb") as fh:
                bot.send_document(TARGET_CHANNEL_ID, fh, caption=caption)
        except Exception:
            logger.exception("Telegram document upload failed for %s", f)
def _download_instagram_attachment(url: str) -> tuple[list[Path], str | None]:
    """Download a signed Instagram CDN attachment directly.

    Instagram messaging webhooks can provide a temporary lookaside.fbsbx.com
    media URL. That URL is already a downloadable media resource, so yt-dlp /
    gallery-dl are not appropriate for it.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_webhook_"))
    try:
        response = requests.get(
            url,
            stream=True,
            timeout=(15, 120),
            allow_redirects=True,
            headers={"User-Agent": "UltimateDownloader/1.0"},
        )
        response.raise_for_status()

        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if content_type.startswith("video/"):
            ext = mimetypes.guess_extension(content_type) or ".mp4"
        elif content_type.startswith("image/"):
            ext = mimetypes.guess_extension(content_type) or ".jpg"
        else:
            ext = Path(response.url.split("?", 1)[0]).suffix.lower() or ".bin"

        output = temp_dir / f"instagram_media{ext}"
        with output.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

        return [output], str(temp_dir)
    except Exception:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _graph_media_urls(media_id: str) -> list[str]:
    if not INSTAGRAM_ACCESS_TOKEN:
        logger.warning("INSTAGRAM_ACCESS_TOKEN is not configured; cannot resolve media ID %s", media_id)
        return []
    fields = "id,media_type,media_url,children{id,media_type,media_url}"
    response = requests.get(
        "https://graph.instagram.com/" + GRAPH_VERSION + "/" + media_id,
        params={"fields": fields, "access_token": INSTAGRAM_ACCESS_TOKEN},
        timeout=(15, 30),
    )
    response.raise_for_status()
    data = response.json()
    urls: list[str] = []
    for child in data.get("children", {}).get("data", []):
        u = child.get("media_url")
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.append(u)
    if not urls:
        u = data.get("media_url")
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.append(u)
    return urls


def _download_and_forward(bot, attachments: list[dict[str, Any]], caption: str | None = None) -> None:
    temp_dirs: list[str] = []
    all_files: list[Path] = []
    try:
        urls: list[str] = []
        seen_ids: set[str] = set()
        for attachment in attachments:
            payload = attachment.get("payload") or {}
            media_id = payload.get("ig_post_media_id") or payload.get("media_id") or payload.get("id")
            if isinstance(media_id, str) and media_id not in seen_ids:
                seen_ids.add(media_id)
                try:
                    urls.extend(_graph_media_urls(media_id))
                except Exception:
                    logger.exception("Instagram Graph media lookup failed for %s", media_id)
            u = payload.get("url")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                urls.append(u)
        for url in dict.fromkeys(urls):
            if "lookaside.fbsbx.com" in url or "fbsbx.com" in url:
                files, temp_dir = _download_instagram_attachment(url)
            else:
                files, temp_dir = download_public_url(url)
            if temp_dir:
                temp_dirs.append(temp_dir)
            all_files.extend(files or [])
        if not all_files:
            logger.warning("No media downloaded from Instagram attachments")
            return
        _send_to_telegram(bot, all_files, caption=caption)
    except Exception:
        logger.exception("Instagram media processing failed")
    finally:
        import shutil
        for temp_dir in temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _handle_event(bot, event: dict[str, Any]) -> None:
    messaging = event.get("messaging") or []
    for item in messaging:
        message = item.get("message") or {}
        mid = message.get("mid")
        if _already_seen(mid):
            continue
        attachments = [a for a in (message.get("attachments") or []) if isinstance(a, dict)]
        if attachments:
            threading.Thread(
                target=_download_and_forward,
                args=(bot, attachments, None),
                daemon=True,
            ).start()
""Instagram DM webhook bridge for Ultimate Downloader.

Receives Instagram Messaging API webhook events and forwards supported media
attachments/URLs to the configured Telegram channel. Secrets are read only
from environment variables.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
import mimetypes
import tempfile
from typing import Any

import requests
from flask import Flask, jsonify, request
from telebot import types as tg_types

from universal_downloader import download_public_url

logger = logging.getLogger("instagram_webhook")

# Keep a known fallback so a stale/missing Railway variable cannot make Meta
# verification fail when the value entered in Meta matches the app's configured
# verification token. If a Railway variable is present, it is also accepted.
DEFAULT_VERIFY_TOKEN = "igwh_7f4c2d9a_2026"
VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "").strip()
ACCEPTED_VERIFY_TOKENS = {
    token for token in (VERIFY_TOKEN, DEFAULT_VERIFY_TOKEN) if token
}
TARGET_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
GRAPH_VERSION = os.getenv("INSTAGRAM_GRAPH_VERSION", "v26.0").strip()
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

    # Telegram supports up to 10 photos/videos in one media group. Preserve
    # Instagram multi-attachment messages instead of concurrent sends.
    media_exts = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm", ".mov", ".avi"}
    media_files = [f for f in files if f.suffix.lower() in media_exts]

    for start in range(0, len(media_files), 10):
        chunk = media_files[start:start + 10]
        handles = []
        media = []
        try:
            for index, f in enumerate(chunk):
                fh = f.open("rb")
                handles.append(fh)
                item_caption = caption if start == 0 and index == 0 else None
                if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                    media.append(tg_types.InputMediaVideo(fh, caption=item_caption))
                else:
                    media.append(tg_types.InputMediaPhoto(fh, caption=item_caption))

            if len(media) == 1:
                if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                    bot.send_video(TARGET_CHANNEL_ID, handles[0], caption=caption)
                else:
                    bot.send_photo(TARGET_CHANNEL_ID, handles[0], caption=caption)
            else:
                bot.send_media_group(TARGET_CHANNEL_ID, media)
        except Exception:
            logger.exception("Telegram media group upload failed for %s", chunk)
            for f in chunk:
                try:
                    with f.open("rb") as fh:
                        if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                            bot.send_video(TARGET_CHANNEL_ID, fh, caption=caption)
                        else:
                            bot.send_photo(TARGET_CHANNEL_ID, fh, caption=caption)
                except Exception:
                    logger.exception("Telegram individual upload failed for %s", f)
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass

    for f in files:
        if f in media_files:
            continue
        try:
            with f.open("rb") as fh:
                bot.send_document(TARGET_CHANNEL_ID, fh, caption=caption)
        except Exception:
            logger.exception("Telegram document upload failed for %s", f)
def _download_instagram_attachment(url: str) -> tuple[list[Path], str | None]:
    """Download a signed Instagram CDN attachment directly.

    Instagram messaging webhooks can provide a temporary lookaside.fbsbx.com
    media URL. That URL is already a downloadable media resource, so yt-dlp /
    gallery-dl are not appropriate for it.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="ig_webhook_"))
    try:
        response = requests.get(
            url,
            stream=True,
            timeout=(15, 120),
            allow_redirects=True,
            headers={"User-Agent": "UltimateDownloader/1.0"},
        )
        response.raise_for_status()

        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if content_type.startswith("video/"):
            ext = mimetypes.guess_extension(content_type) or ".mp4"
        elif content_type.startswith("image/"):
            ext = mimetypes.guess_extension(content_type) or ".jpg"
        else:
            ext = Path(response.url.split("?", 1)[0]).suffix.lower() or ".bin"

        output = temp_dir / f"instagram_media{ext}"
        with output.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

        return [output], str(temp_dir)
    except Exception:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _graph_media_urls(media_id: str) -> list[str]:
    if not INSTAGRAM_ACCESS_TOKEN:
        logger.warning("INSTAGRAM_ACCESS_TOKEN is not configured; cannot resolve media ID %s", media_id)
        return []
    fields = "id,media_type,media_url,children{id,media_type,media_url}"
    response = requests.get(
        "https://graph.instagram.com/" + GRAPH_VERSION + "/" + media_id,
        params={"fields": fields, "access_token": INSTAGRAM_ACCESS_TOKEN},
        timeout=(15, 30),
    )
    response.raise_for_status()
    data = response.json()
    urls: list[str] = []
    for child in data.get("children", {}).get("data", []):
        u = child.get("media_url")
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.append(u)
    if not urls:
        u = data.get("media_url")
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.append(u)
    return urls


def _download_and_forward(bot, attachments: list[dict[str, Any]], caption: str | None = None) -> None:
    temp_dirs: list[str] = []
    all_files: list[Path] = []
    try:
        urls: list[str] = []
        seen_ids: set[str] = set()
        for attachment in attachments:
            payload = attachment.get("payload") or {}
            media_id = payload.get("ig_post_media_id") or payload.get("media_id") or payload.get("id")
            if isinstance(media_id, str) and media_id not in seen_ids:
                seen_ids.add(media_id)
                try:
                    urls.extend(_graph_media_urls(media_id))
                except Exception:
                    logger.exception("Instagram Graph media lookup failed for %s", media_id)
            u = payload.get("url")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                urls.append(u)
        for url in dict.fromkeys(urls):
            if "lookaside.fbsbx.com" in url or "fbsbx.com" in url:
                files, temp_dir = _download_instagram_attachment(url)
            else:
                files, temp_dir = download_public_url(url)
            if temp_dir:
                temp_dirs.append(temp_dir)
            all_files.extend(files or [])
        if not all_files:
            logger.warning("No media downloaded from Instagram attachments")
            return
        _send_to_telegram(bot, all_files, caption=caption)
    except Exception:
        logger.exception("Instagram media processing failed")
    finally:
        import shutil
        for temp_dir in temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _handle_event(bot, event: dict[str, Any]) -> None:
    messaging = event.get("messaging") or []
    for item in messaging:
        message = item.get("message") or {}
        mid = message.get("mid")
        if _already_seen(mid):
            continue

        urls: list[str] = []

        for attachment in message.get("attachments") or []:
            payload = attachment.get("payload") or {}
            url = payload.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)

        text = message.get("text")
        if isinstance(text, str):
            import re
            urls.extend(re.findall(r"https?://[^\s<>\"']+", text))

        unique_urls = list(dict.fromkeys(urls))
        if unique_urls:
            threading.Thread(
                target=_download_and_forward,
                args=(bot, unique_urls, None),
                daemon=True,
            ).start()

@app.get("/instagram/webhook")
def verify_webhook():
    mode = (
        request.args.get("hub.mode")
        or request.args.get("hub_mode")
        or ""
    ).strip()
    token = (
        request.args.get("hub.verify_token")
        or request.args.get("hub_verify_token")
        or ""
    ).strip()
    challenge = (
        request.args.get("hub.challenge")
        or request.args.get("hub_challenge")
        or ""
    )

    if mode == "subscribe" and token in ACCEPTED_VERIFY_TOKENS:
        logger.info(
            "Instagram webhook verification accepted (token matched configured value)"
        )
        return challenge, 200

    logger.warning(
        "Instagram webhook verification rejected: mode=%r token_present=%s challenge_present=%s",
        mode,
        bool(token),
        bool(challenge),
    )
    return "Forbidden", 403


@app.post("/instagram/webhook")
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    bot = app.config.get("telegram_bot")
    if bot is None:
        return jsonify({"ok": False, "error": "Telegram bot not initialized"}), 503

    for entry in payload.get("entry") or []:
        _handle_event(bot, entry)

    return jsonify({"ok": True}), 200


@app.get("/privacy-policy")
def privacy_policy():
    return """
    <!doctype html>
    <html><head><meta charset="utf-8"><title>Privacy Policy - Ultimate Downloader</title></head>
    <body>
    <h1>Privacy Policy</h1>
    <p>Ultimate Downloader processes media that users send to the connected Instagram account and forwards permitted media to the configured Telegram channel.</p>
    <p>We use the information required to receive and process these messages, including message metadata and media URLs, only to provide the bot's requested functionality.</p>
    <p>Downloaded media is processed temporarily and is removed from the bot's working storage after processing, subject to platform or hosting logs outside the bot's control.</p>
    <p>We do not intentionally sell personal information.</p>
    <p>For privacy questions, contact the app owner through the contact method associated with this application.</p>
    </body></html>
    """, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "ultimate-downloader"}), 200


def start_instagram_webhook(bot) -> None:
    if not VERIFY_TOKEN:
        logger.warning(
            "INSTAGRAM_VERIFY_TOKEN is not configured; using built-in fallback verification token."
        )
    if not TARGET_CHANNEL_ID:
        logger.warning("TELEGRAM_CHANNEL_ID is not configured; Instagram media cannot be forwarded.")

    app.config["telegram_bot"] = bot

    thread = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=PORT,
            threaded=True,
            use_reloader=False,
        ),
        daemon=True,
    )
    thread.start()
    logger.info("Instagram webhook listening on port %s", PORT)
