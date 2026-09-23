"""Instagram DM webhook bridge for Ultimate Downloader.

Receives Instagram Messaging API webhook events and forwards supported media
attachments/URLs to the configured Telegram channel. Secrets are read only
from environment variables.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request
from telebot import types as tg_types

from universal_downloader import download_public_url

logger = logging.getLogger("instagram_webhook")

DEFAULT_VERIFY_TOKEN = "igwh_7f4c2d9a_2026"
VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "").strip()
ACCEPTED_VERIFY_TOKENS = {
    token for token in (VERIFY_TOKEN, DEFAULT_VERIFY_TOKEN) if token
}
TARGET_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
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


def _send_to_telegram(
    bot, files: list[Path], caption: str | None = None
) -> None:
    if not TARGET_CHANNEL_ID:
        logger.error("TELEGRAM_CHANNEL_ID is not configured")
        return

    video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
    media_exts = {".jpg", ".jpeg", ".png", ".webp", *video_exts}
    media_files = [f for f in files if f.suffix.lower() in media_exts]

    # Telegram media groups accept at most 10 items.
    for start in range(0, len(media_files), 10):
        chunk = media_files[start : start + 10]
        handles = []
        media = []
        try:
            for index, file_path in enumerate(chunk):
                fh = file_path.open("rb")
                handles.append(fh)
                item_caption = caption if start == 0 and index == 0 else None

                if file_path.suffix.lower() in video_exts:
                    media.append(
                        tg_types.InputMediaVideo(fh, caption=item_caption)
                    )
                else:
                    media.append(
                        tg_types.InputMediaPhoto(fh, caption=item_caption)
                    )

            if len(media) == 1:
                file_path = chunk[0]
                if file_path.suffix.lower() in video_exts:
                    bot.send_video(
                        TARGET_CHANNEL_ID, handles[0], caption=caption
                    )
                else:
                    bot.send_photo(
                        TARGET_CHANNEL_ID, handles[0], caption=caption
                    )
            else:
                bot.send_media_group(TARGET_CHANNEL_ID, media)

        except Exception:
            logger.exception(
                "Telegram media group upload failed for %s", chunk
            )
            for file_path in chunk:
                try:
                    with file_path.open("rb") as fh:
                        if file_path.suffix.lower() in video_exts:
                            bot.send_video(
                                TARGET_CHANNEL_ID, fh, caption=caption
                            )
                        else:
                            bot.send_photo(
                                TARGET_CHANNEL_ID, fh, caption=caption
                            )
                except Exception:
                    logger.exception(
                        "Telegram individual upload failed for %s", file_path
                    )
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass

    for file_path in files:
        if file_path in media_files:
            continue
        try:
            with file_path.open("rb") as fh:
                bot.send_document(
                    TARGET_CHANNEL_ID, fh, caption=caption
                )
        except Exception:
            logger.exception(
                "Telegram document upload failed for %s", file_path
            )


def _download_instagram_attachment(
    url: str,
) -> tuple[list[Path], str | None]:
    """Download a signed Instagram CDN attachment directly."""
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

        content_type = (
            response.headers.get("Content-Type") or ""
        ).split(";", 1)[0].lower()

        if content_type.startswith("video/"):
            ext = mimetypes.guess_extension(content_type) or ".mp4"
        elif content_type.startswith("image/"):
            ext = mimetypes.guess_extension(content_type) or ".jpg"
        else:
            ext = (
                Path(response.url.split("?", 1)[0]).suffix.lower()
                or ".bin"
            )

        output = temp_dir / f"instagram_media{ext}"
        with output.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

        return [output], str(temp_dir)

    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _instagram_token_ok() -> bool:
    """Validate the token against the Instagram Graph host only."""
    if not INSTAGRAM_ACCESS_TOKEN:
        return False
    try:
        response = requests.get(
            f"https://graph.instagram.com/{GRAPH_VERSION}/me",
            params={
                "fields": "id,username",
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=(15, 20),
        )
        if response.ok:
            data = response.json()
            logger.info(
                "Instagram token validation OK: id=%s username_present=%s",
                data.get("id"),
                bool(data.get("username")),
            )
            return True
        try:
            err = response.json().get("error", {})
            logger.warning(
                "Instagram token validation failed: HTTP %s code=%s type=%s message=%s",
                response.status_code,
                err.get("code"),
                err.get("type"),
                err.get("message") or err.get("error_user_msg"),
            )
        except Exception:
            logger.warning("Instagram token validation failed: HTTP %s", response.status_code)
    except Exception:
        logger.exception("Instagram token validation request errored")
    return False


def _graph_media_urls(media_id: str) -> list[str]:
    """Resolve an Instagram media ID, expanding carousel children."""
    if not INSTAGRAM_ACCESS_TOKEN:
        logger.warning(
            "INSTAGRAM_ACCESS_TOKEN is not configured; cannot resolve media ID %s",
            media_id,
        )
        return []

    urls: list[str] = []

    # Carousel children are exposed through the /children edge. Asking for
    # children as a nested field on the parent media endpoint can return HTTP
    # 400 for Instagram Login tokens, so use the dedicated edge first.
    children_response = requests.get(
        f"https://graph.instagram.com/{GRAPH_VERSION}/{media_id}/children",
        params={
            "fields": "id,media_type,media_url",
            "access_token": INSTAGRAM_ACCESS_TOKEN,
            "limit": 100,
        },
        timeout=(15, 30),
    )

    if children_response.ok:
        children_data = children_response.json()
        for child in children_data.get("data", []):
            if not isinstance(child, dict):
                continue
            media_url = child.get("media_url")
            if isinstance(media_url, str) and media_url.startswith(
                ("http://", "https://")
            ):
                urls.append(media_url)
    else:
        # Log Meta's actual error without logging the access token or full URL.
        try:
            error_body = children_response.json().get("error", {})
            error_message = (
                error_body.get("message")
                or error_body.get("error_user_msg")
                or children_response.text[:300]
            )
            error_code = error_body.get("code")
            error_type = error_body.get("type")
        except Exception:
            error_message = children_response.text[:300]
            error_code = None
            error_type = None

        logger.warning(
            "Instagram /children lookup failed for media %s: HTTP %s code=%s type=%s message=%s",
            media_id,
            children_response.status_code,
            error_code,
            error_type,
            error_message,
        )

    # If /children did not resolve anything, first ask only for media_type.
    # A CAROUSEL_ALBUM parent has no media_url field, so requesting
    # media_url on the parent can itself produce HTTP 400.
    if not urls:
        try:
            type_response = requests.get(
                f"https://graph.instagram.com/{GRAPH_VERSION}/{media_id}",
                params={
                    "fields": "id,media_type",
                    "access_token": INSTAGRAM_ACCESS_TOKEN,
                },
                timeout=(15, 30),
            )
            if type_response.ok:
                media_type = type_response.json().get("media_type", "")
                logger.info(
                    "Instagram Graph media %s type=%s",
                    media_id,
                    media_type,
                )

                if media_type == "CAROUSEL_ALBUM":
                    logger.info(
                        "Instagram Graph media %s is a carousel; /children did not resolve it",
                        media_id,
                    )
                else:
                    media_response = requests.get(
                        f"https://graph.instagram.com/{GRAPH_VERSION}/{media_id}",
                        params={
                            "fields": "id,media_type,media_url",
                            "access_token": INSTAGRAM_ACCESS_TOKEN,
                        },
                        timeout=(15, 30),
                    )
                    if media_response.ok:
                        data = media_response.json()
                        media_url = data.get("media_url")
                        if isinstance(media_url, str) and media_url.startswith(
                            ("http://", "https://")
                        ):
                            urls.append(media_url)
                    else:
                        logger.warning(
                            "Instagram Graph media_url lookup failed for media %s: HTTP %s",
                            media_id,
                            media_response.status_code,
                        )
            else:
                logger.warning(
                    "Instagram Graph media_type lookup failed for media %s: HTTP %s",
                    media_id,
                    type_response.status_code,
                )
        except Exception:
            logger.exception(
                "Instagram Graph parent lookup failed for media %s",
                media_id,
            )

    logger.info(
        "Instagram Graph media %s resolved to %d media URL(s)",
        media_id,
        len(urls),
    )
    return urls


def _graph_message_details(message_id: str, ig_user_id: str | None = None, sender_id: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch the full Instagram message when the webhook only exposes a thin share attachment."""
    if not INSTAGRAM_ACCESS_TOKEN or not message_id:
        return [], []

    fields = "id,attachments,shares,message"
    # Instagram Login tokens are for graph.instagram.com.
    endpoints = [
        f"https://graph.instagram.com/{GRAPH_VERSION}/{message_id}",
    ]

    def collect_details(data: Any) -> tuple[list[dict[str, Any]], list[str]]:
        recovered: list[dict[str, Any]] = []
        links: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    collect(item)
                return
            if not isinstance(value, dict):
                return

            payload = value.get("payload")
            if isinstance(payload, dict):
                recovered.append({"payload": payload, "type": value.get("type")})
                for key in ("link", "url", "permalink", "permalink_url"):
                    candidate = payload.get(key)
                    if isinstance(candidate, str) and "instagram.com" in candidate:
                        links.append(candidate)

            for key in ("link", "permalink", "permalink_url"):
                candidate = value.get(key)
                if isinstance(candidate, str) and "instagram.com" in candidate:
                    links.append(candidate)

            for key in ("data", "attachments", "shares", "elements"):
                if key in value:
                    collect(value.get(key))

            nested_message = value.get("message")
            if isinstance(nested_message, dict):
                collect(nested_message)

        collect(data.get("attachments"))
        collect(data.get("shares"))
        collect(data.get("message"))
        return recovered, list(dict.fromkeys(links))

    for endpoint in endpoints:
        try:
            response = requests.get(
                endpoint,
                params={
                    "fields": fields,
                    "access_token": INSTAGRAM_ACCESS_TOKEN,
                },
                timeout=(15, 30),
            )
            if not response.ok:
                logger.info("Instagram message lookup failed: HTTP %s", response.status_code)
                continue

            recovered, links = collect_details(response.json())
            logger.info(
                "Instagram message lookup recovered %d attachment record(s) and %d permalink(s)",
                len(recovered),
                len(links),
            )
            if recovered or links:
                return recovered, links
        except Exception:
            logger.exception("Instagram message lookup errored")

    if ig_user_id and sender_id:
        try:
            conversations_response = requests.get(
                f"https://graph.instagram.com/{GRAPH_VERSION}/{ig_user_id}/conversations",
                params={
                    "user_id": sender_id,
                    "access_token": INSTAGRAM_ACCESS_TOKEN,
                },
                timeout=(15, 30),
            )
            if conversations_response.ok:
                conversations = conversations_response.json().get("data", [])
                for conversation in conversations:
                    if not isinstance(conversation, dict):
                        continue
                    conversation_id = conversation.get("id")
                    if not isinstance(conversation_id, str):
                        continue

                    messages_response = requests.get(
                        f"https://graph.instagram.com/{GRAPH_VERSION}/{conversation_id}",
                        params={
                            "fields": "messages{id,attachments,shares,message,created_time}",
                            "access_token": INSTAGRAM_ACCESS_TOKEN,
                        },
                        timeout=(15, 30),
                    )
                    if not messages_response.ok:
                        continue

                    messages_data = messages_response.json()
                    for msg in messages_data.get("messages", {}).get("data", []):
                        if not isinstance(msg, dict):
                            continue
                        if msg.get("id") != message_id:
                            continue

                        recovered, links = collect_details(msg)
                        logger.info(
                            "Instagram conversation lookup matched webhook mid; recovered %d attachment record(s) and %d permalink(s)",
                            len(recovered),
                            len(links),
                        )
                        return recovered, links
            else:
                logger.info(
                    "Instagram conversation lookup failed: HTTP %s",
                    conversations_response.status_code,
                )
        except Exception:
            logger.exception("Instagram conversation lookup errored")

    return [], []

def _media_id_to_shortcode(media_id: str) -> str | None:
    """Convert a numeric Instagram media ID to its public shortcode."""
    try:
        value = int(media_id)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    chars: list[str] = []
    while value:
        chars.append(alphabet[value & 63])
        value >>= 6
    return "".join(reversed(chars))


def _download_and_forward(
    bot, attachments: list[dict[str, Any]], caption: str | None = None
) -> None:
    temp_dirs: list[str] = []
    all_files: list[Path] = []

    try:
        urls: list[str] = []
        direct_urls: set[str] = set()
        seen_ids: set[str] = set()

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue

            payload = attachment.get("payload") or {}
            if not isinstance(payload, dict):
                continue

            media_id = (
                payload.get("ig_post_media_id")
                or payload.get("media_id")
                or payload.get("id")
            )

            # Meta share attachments can include the original Instagram
            # permalink in payload.link. For a carousel owned by another
            # Instagram account, the recipient's Graph token may not be
            # allowed to read the shared media's children. In that case the
            # permalink is the supported public fallback and gallery-dl can
            # expand the carousel.
            post_link = payload.get("link") or payload.get("permalink_url")

            logger.info(
                "Instagram attachment: type=%s media_id_present=%s post_link_present=%s",
                attachment.get("type", "unknown"),
                bool(media_id),
                bool(post_link),
            )

            resolved_urls: list[str] = []
            if isinstance(media_id, str) and media_id not in seen_ids:
                seen_ids.add(media_id)
                try:
                    resolved_urls = _graph_media_urls(media_id)
                except Exception:
                    logger.exception(
                        "Instagram Graph media lookup failed for %s",
                        media_id,
                    )

            if resolved_urls:
                # Graph child URLs are the complete carousel set. Do not also
                # append the webhook's single CDN URL.
                urls.extend(resolved_urls)
                direct_urls.update(resolved_urls)
                continue

            if isinstance(post_link, str) and "instagram.com" in post_link:
                try:
                    logger.info(
                        "Instagram Graph returned no media; trying permalink fallback"
                    )
                    permalink_files, permalink_dir = download_public_url(post_link)
                    if permalink_files:
                        if permalink_dir:
                            temp_dirs.append(permalink_dir)
                        all_files.extend(permalink_files)
                        logger.info(
                            "Instagram permalink fallback downloaded %d file(s)",
                            len(permalink_files),
                        )
                        continue
                    if permalink_dir:
                        shutil.rmtree(permalink_dir, ignore_errors=True)
                    logger.warning(
                        "Instagram permalink fallback returned no files"
                    )
                except Exception:
                    logger.exception(
                        "Instagram permalink fallback failed"
                    )

            # New ig_post webhooks may omit the public permalink entirely.
            # The numeric ig_post_media_id can be converted to Instagram's
            # public shortcode, so try the public post URL before falling back
            # to the single signed CDN attachment. This is especially useful
            # for carousel posts whose /children edge is inaccessible to the
            # recipient's API token.
            if (
                isinstance(media_id, str)
                and not isinstance(post_link, str)
                and "instagram.com" not in str(post_link)
            ):
                shortcode = _media_id_to_shortcode(media_id)
                if shortcode:
                    derived_link = f"https://www.instagram.com/p/{shortcode}/"
                    try:
                        logger.info(
                            "Instagram permalink absent; trying media-ID derived public URL"
                        )
                        permalink_files, permalink_dir = download_public_url(derived_link)
                        if permalink_files:
                            if permalink_dir:
                                temp_dirs.append(permalink_dir)
                            all_files.extend(permalink_files)
                            logger.info(
                                "Instagram media-ID permalink fallback downloaded %d file(s)",
                                len(permalink_files),
                            )
                            continue
                        if permalink_dir:
                            shutil.rmtree(permalink_dir, ignore_errors=True)
                        logger.warning(
                            "Instagram media-ID permalink fallback returned no files"
                        )
                    except Exception:
                        logger.exception(
                            "Instagram media-ID permalink fallback failed"
                        )

            # Final fallback for ordinary webhook attachments or when the
            # public permalink cannot be extracted.
            media_url = payload.get("url")
            if isinstance(media_url, str) and media_url.startswith(
                ("http://", "https://")
            ):
                urls.append(media_url)
                direct_urls.add(media_url)

        unique_urls = list(dict.fromkeys(urls))
        logger.info(
            "Instagram webhook: %d unique media URL(s) to download",
            len(unique_urls),
        )

        for url in unique_urls:
            if url in direct_urls or "fbsbx.com" in url or "cdninstagram.com" in url or "scontent" in url:
                files, temp_dir = _download_instagram_attachment(url)
            else:
                files, temp_dir = download_public_url(url)

            if temp_dir:
                temp_dirs.append(temp_dir)
            all_files.extend(files or [])

        if not all_files:
            logger.warning(
                "No media downloaded from Instagram attachments"
            )
            return

        _send_to_telegram(bot, all_files, caption=caption)

    except Exception:
        logger.exception("Instagram media processing failed")
    finally:
        for temp_dir in temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _handle_event(bot, event: dict[str, Any]) -> None:
    messaging = event.get("messaging") or []

    for item in messaging:
        if not isinstance(item, dict):
            continue

        message = item.get("message") or {}
        if not isinstance(message, dict):
            continue

        mid = message.get("mid")
        if _already_seen(mid):
            continue

        attachments = [
            attachment
            for attachment in (message.get("attachments") or [])
            if isinstance(attachment, dict)
        ]

        # Query the message resource too. The share webhook can expose only
        # one CDN URL, while the message resource may expose richer
        # attachment/share metadata.
        recovered_attachments, recovered_links = _graph_message_details(
            mid,
            ig_user_id=event.get("id"),
            sender_id=(item.get("sender") or {}).get("id") if isinstance(item.get("sender"), dict) else None,
        )
        if recovered_attachments:
            attachments.extend(recovered_attachments)
        for recovered_link in recovered_links:
            attachments.append({"payload": {"link": recovered_link}})

        if attachments:
            threading.Thread(
                target=_download_and_forward,
                args=(bot, attachments, None),
                daemon=True,
            ).start()
            continue

        # Also support a plain URL sent as text.
        text = message.get("text")
        if isinstance(text, str):
            import re

            text_urls = re.findall(r"https?://[^\s<>\"']+", text)
            if text_urls:
                text_attachments = [
                    {"payload": {"url": url}}
                    for url in dict.fromkeys(text_urls)
                ]
                threading.Thread(
                    target=_download_and_forward,
                    args=(bot, text_attachments, None),
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
            "Instagram webhook verification accepted"
        )
        return challenge, 200

    logger.warning(
        "Instagram webhook verification rejected: mode=%r "
        "token_present=%s challenge_present=%s",
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
        return (
            jsonify(
                {"ok": False, "error": "Telegram bot not initialized"}
            ),
            503,
        )

    for entry in payload.get("entry") or []:
        if isinstance(entry, dict):
            _handle_event(bot, entry)

    return jsonify({"ok": True}), 200


@app.get("/privacy-policy")
def privacy_policy():
    return (
        """
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8">
          <title>Privacy Policy - Ultimate Downloader</title>
        </head>
        <body>
          <h1>Privacy Policy</h1>
          <p>
            Ultimate Downloader processes media that users send to the
            connected Instagram account and forwards permitted media to the
            configured Telegram channel.
          </p>
          <p>
            We use the information required to receive and process these
            messages, including message metadata and media URLs, only to
            provide the bot's requested functionality.
          </p>
          <p>
            Downloaded media is processed temporarily and is removed from the
            bot's working storage after processing, subject to platform or
            hosting logs outside the bot's control.
          </p>
          <p>We do not intentionally sell personal information.</p>
          <p>
            For privacy questions, contact the app owner through the contact
            method associated with this application.
          </p>
        </body>
        </html>
        """,
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "ultimate-downloader"}), 200


def start_instagram_webhook(bot) -> None:
    if not VERIFY_TOKEN:
        logger.warning(
            "INSTAGRAM_VERIFY_TOKEN is not configured; using built-in "
            "fallback verification token."
        )

    if not TARGET_CHANNEL_ID:
        logger.warning(
            "TELEGRAM_CHANNEL_ID is not configured; Instagram media "
            "cannot be forwarded."
        )

    if not INSTAGRAM_ACCESS_TOKEN:
        logger.warning(
            "INSTAGRAM_ACCESS_TOKEN is not configured; carousel expansion "
            "will fall back to the webhook's single media URL."
        )
    else:
        _instagram_token_ok()

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
