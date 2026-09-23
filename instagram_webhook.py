"""Instagram DM webhook bridge for Ultimate Downloader.

Receives Instagram Messaging API webhook events and forwards supported media
attachments/URLs to the configured Telegram channel. Secrets are read only
from environment variables.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request

try:
    from instaloader import Instaloader, Post
except ImportError:
    Instaloader = None
    Post = None

try:
    from instagrapi import Client as InstaGrapiClient
except ImportError:
    InstaGrapiClient = None
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
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "").strip()
INSTAGRAM_SESSION_COOKIE = os.getenv("INSTAGRAM_SESSION_COOKIE", "").strip()
INSTAGRAM_CSRF_TOKEN = os.getenv("INSTAGRAM_CSRF_TOKEN", "").strip()
INSTAGRAM_WWW_CLAIM = os.getenv("INSTAGRAM_WWW_CLAIM", "").strip()
INSTAGRAM_BROWSER_RESOLVER = os.getenv("INSTAGRAM_BROWSER_RESOLVER", "false").strip().lower() in {"1", "true", "yes", "on"}
PLAYWRIGHT_EXECUTABLE_PATH = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH", "").strip()
INSTAGRAM_BROWSER_WAIT_SECONDS = float(os.getenv("INSTAGRAM_BROWSER_WAIT_SECONDS", "6"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or ("cookies.txt" if Path("cookies.txt").exists() else "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ultimate-downloader-production.up.railway.app").strip().rstrip("/")
GRAPH_VERSION = os.getenv("INSTAGRAM_GRAPH_VERSION", "v26.0").strip()
PORT = int(os.getenv("PORT", "8080"))

app = Flask(__name__)
_seen_mids: set[str] = set()
_seen_lock = threading.Lock()

_instagr_api_client = None
_instagr_api_lock = threading.Lock()


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


def _instagram_cookie_header() -> str:
    """Build Instagram cookies dynamically, including Railway COOKIES_CONTENT."""
    if INSTAGRAM_SESSION_COOKIE:
        return INSTAGRAM_SESSION_COOKIE

    # main.py writes COOKIES_CONTENT to cookies.txt after imports happen, so
    # do not rely only on the module-level COOKIES_FILE value.
    cookie_path = COOKIES_FILE
    if not cookie_path and Path("cookies.txt").exists():
        cookie_path = "cookies.txt"

    if cookie_path:
        try:
            from http.cookiejar import MozillaCookieJar

            jar = MozillaCookieJar(cookie_path)
        jar.load(ignore_discard=True, ignore_expires=True)
        pairs = []
        for cookie in jar:
            domain = (cookie.domain or "").lower()
            if "instagram.com" in domain:
                pairs.append(f"{cookie.name}={cookie.value}")
            return "; ".join(pairs)
        except Exception:
            logger.debug("Could not load Instagram cookies from %s", cookie_path, exc_info=True)

    # Also accept a Netscape cookies.txt payload directly from Railway.
    # This is useful when the file is created by main.py only after this
    # module has already been imported.
    cookies_content = os.getenv("COOKIES_CONTENT", "").strip()
    if cookies_content:
        try:
            pairs = []
            for line in cookies_content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) >= 7:
                    name, value = fields[5], fields[6]
                    if name and value:
                        pairs.append(f"{name}={value}")
            if pairs:
                return "; ".join(pairs)
        except Exception:
            logger.debug("Could not parse COOKIES_CONTENT", exc_info=True)

    return ""


def _instagram_mobile_headers(host: str) -> dict[str, str]:
    cookie = _instagram_cookie_header()
    headers = {
        "Accept": "*/*",
        "X-IG-App-ID": "936619743392459",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; SM-G981B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.162 "
            "Mobile Safari/537.36 Instagram 390.0.0.0.74 Android "
            "(30/11; 420dpi; 1080x2400; samsung; SM-G991B; o1s; "
            "exynos2100; en_US; 300000000)"
        ),
        "Referer": "https://www.instagram.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    if INSTAGRAM_CSRF_TOKEN:
        headers["X-CSRFToken"] = INSTAGRAM_CSRF_TOKEN
    if INSTAGRAM_WWW_CLAIM:
        headers["X-IG-WWW-Claim"] = INSTAGRAM_WWW_CLAIM
    if host == "www.instagram.com":
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers



def _instagram_direct_shared_post_urls(
    sender_id: str | None,
    webhook_mid: str | None = None,
) -> list[str]:
    """Recover the canonical Instagram URL from the actual DM message.

    The Messaging webhook can expose only a signed preview/CDN URL for an
    ig_post share. Instagram's authenticated Direct API can contain the
    richer XMA/share object, including its original target URL. This resolver
    reads the bot account's own inbox using the existing session cookie and
    extracts that target URL before falling back to media-id resolvers.
    """
    global _instagr_api_client
    logger.info(
        "Instagram Direct resolver start: sender_id_present=%s webhook_mid_present=%s instagrapi_available=%s",
        bool(sender_id),
        bool(webhook_mid),
        InstaGrapiClient is not None,
    )
    if InstaGrapiClient is None or not sender_id:
        logger.warning(
            "Instagram Direct resolver skipped: sender_id=%r instagrapi_available=%s",
            sender_id,
            InstaGrapiClient is not None,
        )
        return []

    cookie_header = _instagram_cookie_header()
    if not cookie_header:
        logger.info("Instagram Direct resolver skipped: no session cookie")
        return []

    try:
        cookies: dict[str, str] = {}
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()

        sessionid = cookies.get("sessionid")
        if not sessionid:
            logger.warning("Instagram Direct resolver skipped: sessionid cookie missing")
            return []

        with _instagr_api_lock:
            if _instagr_api_client is None:
                client = InstaGrapiClient()
                client.set_user_agent(
                    "Instagram 390.0.0.0.74 Android (30/11; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US)"
                )
                client.login_by_sessionid(sessionid)
                _instagr_api_client = client
            client = _instagr_api_client

        def public_instagram_url(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            value = value.strip()
            if value.startswith(("http://", "https://")) and "instagram.com/" in value:
                return value.split("?", 1)[0]
            return None

        def collect(obj: Any, out: list[str]) -> None:
            if obj is None:
                return
            if isinstance(obj, str):
                url = public_instagram_url(obj)
                if url:
                    out.append(url)
                return
            if isinstance(obj, dict):
                for key, value in obj.items():
                    key_l = str(key).lower()
                    if key_l in {
                        "target_url", "url", "link", "permalink",
                        "permalink_url", "web_uri", "weburl",
                    }:
                        url = public_instagram_url(value)
                        if url:
                            out.append(url)
                    if isinstance(value, (dict, list)):
                        collect(value, out)
                return
            if isinstance(obj, (list, tuple)):
                for value in obj:
                    collect(value, out)

        def message_urls(dm: Any) -> list[str]:
            found: list[str] = []
            for attr in (
                "link",
                "xma_share",
                "media_share",
                "reel_share",
                "story_share",
                "felix_share",
                "clip",
                "generic_xma",
                "raw_xma",
            ):
                try:
                    collect(getattr(dm, attr, None), found)
                except Exception:
                    pass
            return list(dict.fromkeys(found))

        threads: list[Any] = []
        try:
            participant_id = int(str(sender_id))
            thread = client.direct_thread_by_participants([participant_id])
            if thread:
                threads.append(thread)
        except Exception:
            logger.info(
                "Instagram Direct participant lookup failed sender_id=%s",
                sender_id,
                exc_info=True,
            )

        if not threads:
            try:
                threads.extend(
                    client.direct_threads(amount=30, thread_message_limit=15)
                    or []
                )
            except Exception:
                logger.info("Instagram Direct inbox scan failed", exc_info=True)

        for thread in threads:
            try:
                messages = list(
                    getattr(thread, "messages", None)
                    or client.direct_messages(thread.id, amount=30)
                    or []
                )
            except Exception:
                logger.info(
                    "Instagram Direct message fetch failed thread=%s",
                    getattr(thread, "id", None),
                    exc_info=True,
                )
                continue

            logger.info(
                "Instagram Direct thread fetched: thread=%s messages=%d",
                getattr(thread, "id", None),
                len(messages),
            )
            exact = []
            recent_sender = []
            for dm in messages:
                dm_id = str(getattr(dm, "id", "") or "")
                dm_user_id = str(getattr(dm, "user_id", "") or "")
                urls = message_urls(dm)
                if urls:
                    logger.info(
                        "Instagram Direct share candidate: dm_id=%s item_type=%s urls=%d",
                        dm_id,
                        getattr(dm, "item_type", None),
                        len(urls),
                    )
                if not urls:
                    continue
                if webhook_mid and dm_id == str(webhook_mid):
                    exact.append((dm, urls))
                if sender_id and dm_user_id == str(sender_id):
                    recent_sender.append((dm, urls))

            candidates = exact or recent_sender
            if candidates:
                dm, urls = candidates[-1]
                logger.info(
                    "Instagram Direct resolver recovered %d original URL(s): "
                    "thread=%s exact_mid=%s item_type=%s",
                    len(urls),
                    getattr(thread, "id", None),
                    bool(exact),
                    getattr(dm, "item_type", None),
                )
                return urls

        logger.warning(
            "Instagram Direct resolver found no canonical share URL: sender_id=%s webhook_mid=%s",
            sender_id,
            webhook_mid,
        )
    except Exception:
        logger.exception("Instagram Direct shared-post resolver failed")
    return []


def _instagram_private_api_media_urls(media_id: str) -> list[str]:
    """Resolve a shared media ID through Instagram's private/mobile API.

    Uses only the existing Instagram session cookie from Railway. This path
    is intentionally separate from Instaloader because Instagram has recently
    rotated/broken several web GraphQL endpoints; instagrapi exposes the
    private media_info_v1/media_info_v2 fallbacks and understands albums.
    """
    global _instagr_api_client
    if InstaGrapiClient is None or not media_id or not media_id.isdigit():
        return []

    cookie_header = _instagram_cookie_header()
    if not cookie_header:
        logger.info("Instagram private API resolver skipped: no session cookie")
        return []

    try:
        cookies: dict[str, str] = {}
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()

        sessionid = cookies.get("sessionid")
        if not sessionid:
            logger.warning("Instagram private API resolver skipped: sessionid cookie missing")
            return []

        with _instagr_api_lock:
            if _instagr_api_client is None:
                client = InstaGrapiClient()
                client.set_user_agent(
                    "Instagram 390.0.0.0.74 Android (30/11; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US)"
                )
                client.login_by_sessionid(sessionid)
                _instagr_api_client = client
            client = _instagr_api_client

        media = None
        # v1 is the normal private media endpoint; v2 is a useful fallback
        # for media that the v1 endpoint rejects.
        for resolver_name in ("media_info_v1", "media_info_v2"):
            resolver = getattr(client, resolver_name, None)
            if not callable(resolver):
                continue
            try:
                media = resolver(media_id)
                if media is not None:
                    logger.info("Instagram private API %s succeeded for media_id=%s", resolver_name, media_id)
                    break
            except Exception:
                logger.info("Instagram private API %s failed for media_id=%s", resolver_name, media_id, exc_info=True)

        if media is None:
            return []

        urls: list[str] = []
        resources = getattr(media, "resources", None) or []
        for resource in resources:
            media_type = getattr(resource, "media_type", None)
            candidate = getattr(resource, "video_url", None) if media_type == 2 else getattr(resource, "thumbnail_url", None)
            if candidate:
                value = str(candidate)
                if value.startswith(("http://", "https://")):
                    urls.append(value)

        if not urls:
            video = getattr(media, "video_url", None)
            if video:
                urls.append(str(video))

        if not urls:
            image_versions = getattr(media, "image_versions2", None)
            candidates = getattr(image_versions, "candidates", None) if image_versions else None
            if candidates:
                best = max(
                    candidates,
                    key=lambda x: (getattr(x, "width", 0) or 0) * (getattr(x, "height", 0) or 0),
                )
                value = getattr(best, "url", None)
                if value:
                    urls.append(str(value))

        urls = list(dict.fromkeys(u for u in urls if u.startswith(("http://", "https://"))))
        if urls:
            logger.info(
                "Instagram private API resolved %d media URL(s), media_type=%s",
                len(urls),
                getattr(media, "media_type", None),
            )
        return urls
    except Exception:
        logger.exception("Instagram private API resolver failed for media_id=%s", media_id)
        return []


def _instagram_instaloader_media_urls(media_id: str) -> list[str]:
    """Resolve a shared Instagram media ID using an authenticated web session.

    This is the primary non-Graph resolver for shared posts. Instaloader's
    current Post.from_mediaid() converts the numeric ID to the canonical
    shortcode and fetches current post metadata, including all sidecar
    children. The existing Instagram cookie is reused; no password is stored.
    """
    if Post is None or Instaloader is None or not media_id or not media_id.isdigit():
        return []

    cookie_header = _instagram_cookie_header()
    if not cookie_header:
        logger.info("Instagram Instaloader resolver skipped: no session cookie")
        return []

    try:
        cookie_data: dict[str, str] = {}
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookie_data[name.strip()] = value.strip()
        cookie_data.setdefault("csrftoken", "")

        username = os.getenv("INSTAGRAM_USERNAME", "vikatanathan").strip() or "vikatanathan"
        loader = Instaloader(
            sleep=False,
            quiet=True,
            request_timeout=45,
            iphone_support=True,
        )
        loader.context.load_session(username, cookie_data)

        post = Post.from_mediaid(loader.context, int(media_id))
        urls: list[str] = []

        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                candidate = node.video_url if node.is_video else node.display_url
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    urls.append(candidate)
        elif post.is_video:
            candidate = post.video_url
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                urls.append(candidate)
        else:
            candidate = post.url
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                urls.append(candidate)

        urls = list(dict.fromkeys(urls))
        if urls:
            logger.info(
                "Instagram Instaloader resolver succeeded: media_id=%s shortcode=%s type=%s items=%d",
                media_id,
                post.shortcode,
                post.typename,
                len(urls),
            )
        else:
            logger.warning(
                "Instagram Instaloader resolver returned no media: media_id=%s shortcode=%s type=%s",
                media_id,
                post.shortcode,
                post.typename,
            )
        return urls
    except Exception:
        logger.exception("Instagram Instaloader resolver failed for media_id=%s", media_id)
        return []


def _instagram_mobile_media_urls(media_id: str) -> list[str]:
    """Resolve a shared post through Instagram's media-info API.

    Instagram has recently returned 403 from the i.instagram.com mobile
    endpoint when no authenticated web session is present. Try the current
    www endpoint first, then the mobile host, and reuse the optional
    Instagram cookies already supplied to the downloader.
    """
    if not media_id or not media_id.isdigit():
        return []

    endpoints = [
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
        f"https://i.instagram.com/api/v1/media/{media_id}/info/",
    ]

    for endpoint in endpoints:
        host = "www.instagram.com" if "www.instagram.com" in endpoint else "i.instagram.com"
        try:
            response = requests.get(
                endpoint,
                headers=_instagram_mobile_headers(host),
                timeout=(15, 30),
            )
            if not response.ok:
                logger.info(
                    "Instagram media-info lookup failed host=%s HTTP %s cookie_present=%s",
                    host,
                    response.status_code,
                    bool(_instagram_cookie_header()),
                )
                continue

            content_type = (response.headers.get("Content-Type") or "").lower()
            if "json" not in content_type:
                logger.info(
                    "Instagram media-info returned non-JSON host=%s HTTP=%s content_type=%s body_prefix=%r",
                    host,
                    response.status_code,
                    content_type,
                    response.text[:160],
                )
                continue

            try:
                data = response.json()
            except ValueError:
                logger.info(
                    "Instagram media-info returned invalid JSON host=%s HTTP=%s body_prefix=%r",
                    host,
                    response.status_code,
                    response.text[:160],
                )
                continue

            if not isinstance(data, dict):
                continue
            items = data.get("items") or []
            if not items or not isinstance(items[0], dict):
                continue

            item = items[0]
            media_items = item.get("carousel_media")
            if not isinstance(media_items, list):
                media_items = [item]

            urls: list[str] = []
            for media in media_items:
                if not isinstance(media, dict):
                    continue

                videos = media.get("video_versions") or []
                found_video = False
                if isinstance(videos, list):
                    for candidate in videos:
                        if isinstance(candidate, dict):
                            url = candidate.get("url")
                            if isinstance(url, str) and url.startswith(("http://", "https://")):
                                urls.append(url)
                                found_video = True
                                break

                if found_video:
                    continue

                image_versions = media.get("image_versions2") or {}
                images = image_versions.get("candidates", []) if isinstance(image_versions, dict) else []
                if isinstance(images, list):
                    for candidate in images:
                        if isinstance(candidate, dict):
                            url = candidate.get("url")
                            if isinstance(url, str) and url.startswith(("http://", "https://")):
                                urls.append(url)
                                break

            urls = list(dict.fromkeys(urls))
            if urls:
                logger.info(
                    "Instagram media-info resolved %d media URL(s), carousel=%s host=%s cookie_present=%s",
                    len(urls),
                    len(media_items) > 1,
                    host,
                    bool(_instagram_cookie_header()),
                )
                return urls
        except Exception:
            logger.exception("Instagram media-info lookup errored host=%s", host)

    return []

def _graph_attachment_edge(message_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Try the message attachments edge instead of the media /children edge."""
    if not INSTAGRAM_ACCESS_TOKEN or not message_id:
        return [], []

    endpoints = [
        f"https://graph.instagram.com/{GRAPH_VERSION}/{message_id}/attachments",
        f"https://graph.facebook.com/{GRAPH_VERSION}/{message_id}/attachments",
    ]

    def collect(value: Any, recovered: list[dict[str, Any]], links: list[str]) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item, recovered, links)
            return
        if not isinstance(value, dict):
            return
        payload = value.get("payload")
        if isinstance(payload, dict):
            recovered.append({"payload": payload, "type": value.get("type")})
            for key in ("link", "url", "permalink", "permalink_url"):
                v = payload.get(key)
                if isinstance(v, str) and "instagram.com" in v:
                    links.append(v)
        for key in ("link", "url", "permalink", "permalink_url"):
            v = value.get(key)
            if isinstance(v, str) and "instagram.com" in v:
                links.append(v)
        for key in ("data", "attachments", "shares", "elements"):
            if key in value:
                collect(value[key], recovered, links)

    for endpoint in endpoints:
        for fields in ("id,type,payload,link,url,title", "id,type,payload", "id,type"):
            try:
                response = requests.get(
                    endpoint,
                    params={
                        "fields": fields,
                        "platform": "instagram",
                        "access_token": INSTAGRAM_ACCESS_TOKEN,
                    },
                    timeout=(15, 30),
                )
                if not response.ok:
                    continue
                recovered: list[dict[str, Any]] = []
                links: list[str] = []
                collect(response.json(), recovered, links)
                links=list(dict.fromkeys(links))
                if recovered or links:
                    logger.info(
                        "Instagram message attachments edge recovered %d attachment record(s) and %d permalink(s)",
                        len(recovered), len(links),
                    )
                    return recovered, links
            except Exception:
                logger.exception("Instagram message attachments edge lookup errored")

    logger.info("Instagram message attachments edge returned no richer share metadata")
    return [], []


def _graph_message_details(message_id: str, ig_user_id: str | None = None, sender_id: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch the full Instagram message when the webhook only exposes a thin share attachment."""
    if not INSTAGRAM_ACCESS_TOKEN or not message_id:
        return [], []

    fields = "id,attachments,shares,message"
    edge_recovered, edge_links = _graph_attachment_edge(message_id)
    if edge_recovered or edge_links:
        return edge_recovered, edge_links

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


def _instagram_browser_shared_post_urls(
    sender_id: str | None = None,
    webhook_mid: str | None = None,
) -> list[str]:
    """Use a real Instagram web session to inspect Direct-message network data.

    This is an optional resolver. It opens Instagram's Direct inbox with the
    existing session cookie, captures JSON responses used by the web client,
    recursively separates Instagram post/reel URLs from the message payload,
    and returns only canonical media links. No credentials are hard-coded.
    """
    if not INSTAGRAM_BROWSER_RESOLVER:
        return []

    cookie_header = _instagram_cookie_header()
    if not cookie_header:
        logger.info(
            "Instagram browser resolver skipped: no authenticated session; "
            "continuing with webhook/Graph/public resolvers"
        )
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Instagram browser resolver skipped: Playwright is not installed")
        return []

    def cookie_pairs(header: str) -> list[dict[str, Any]]:
        result = []
        for part in header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name:
                continue
            result.append({
                "name": name,
                "value": value,
                "domain": ".instagram.com",
                "path": "/",
            })
        return result

    def collect_urls(value: Any, out: list[str]) -> None:
        if isinstance(value, str):
            value = value.strip()
            if (
                value.startswith(("http://", "https://"))
                and "instagram.com/" in value
                and re.search(r"/(?:p|reel|tv)/[A-Za-z0-9_-]+", value)
            ):
                out.append(value.split("?", 1)[0])
            return
        if isinstance(value, dict):
            for key, item in value.items():
                # Shared-post objects commonly use link/url/web_uri fields,
                # but recursively scanning the whole JSON keeps this tolerant
                # of Instagram's changing response shapes.
                if key in {"link", "url", "web_uri", "target_url", "permalink", "permalink_url"}:
                    collect_urls(item, out)
                elif isinstance(item, (dict, list)):
                    collect_urls(item, out)
            return
        if isinstance(value, list):
            for item in value:
                collect_urls(item, out)

    captured: list[tuple[str, Any]] = []

    try:
        with sync_playwright() as pw:
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            }
            if PLAYWRIGHT_EXECUTABLE_PATH:
                launch_kwargs["executable_path"] = PLAYWRIGHT_EXECUTABLE_PATH

            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; K) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0 Mobile Safari/537.36"
                ),
                viewport={"width": 412, "height": 915},
            )
            context.add_cookies(cookie_pairs(cookie_header))
            page = context.new_page()

            def on_response(response: Any) -> None:
                try:
                    url = response.url
                    if "/direct_v2/" not in url and "/api/v1/direct" not in url:
                        return
                    ctype = (response.headers.get("content-type") or "").lower()
                    if "json" not in ctype:
                        return
                    # response.json() is intentionally avoided here because
                    # Playwright versions differ; body+json.loads is stable.
                    import json as _json
                    data = _json.loads(response.body().decode("utf-8", "replace"))
                    captured.append((url, data))
                except Exception:
                    pass

            page.on("response", on_response)

            page.goto(
                "https://www.instagram.com/direct/inbox/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(max(1000, int(INSTAGRAM_BROWSER_WAIT_SECONDS * 1000)))

            # Also inspect the rendered DOM. Some shared-post links are added
            # client-side without appearing in the initial API response.
            try:
                hrefs = page.locator('a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]').evaluate_all(
                    "(els) => els.map(e => e.href)"
                )
                captured.append(("dom", hrefs))
            except Exception:
                pass

            browser.close()

        urls: list[str] = []
        for _, data in captured:
            collect_urls(data, urls)

        urls = list(dict.fromkeys(urls))

        # If the exact webhook message ID appears in captured JSON, prefer
        # links from that message. This prevents an unrelated recent DM from
        # being selected when the inbox contains many shared posts.
        if webhook_mid:
            exact_urls: list[str] = []
            for url, data in captured:
                text = str(data)
                if str(webhook_mid) in text:
                    collect_urls(data, exact_urls)
            exact_urls = list(dict.fromkeys(exact_urls))
            if exact_urls:
                urls = exact_urls

        logger.info(
            "Instagram browser resolver captured=%d response payload(s), recovered=%d canonical URL(s), sender_id_present=%s exact_mid=%s",
            len(captured),
            len(urls),
            bool(sender_id),
            bool(webhook_mid and any(str(webhook_mid) in str(data) for _, data in captured)),
        )
        return urls
    except Exception:
        logger.exception("Instagram browser shared-post resolver failed")
        return []


def _extract_instagram_links(value: Any) -> list[str]:
    """Recursively extract canonical Instagram post/reel URLs from webhook data."""
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            for match in re.findall(
                r'https?://(?:www\\.)?instagram\\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+(?:/)?',
                item,
                flags=re.I,
            ):
                found.append(match.rstrip("/"))
            return
        if isinstance(item, dict):
            for child in item.values():
                walk(child)
            return
        if isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return list(dict.fromkeys(found))


def _send_instagram_dm_inspection(
    bot,
    message_data: dict[str, Any] | None,
    message_id: str | None = None,
) -> None:
    """Send a redacted JSON inspection file to Telegram for debugging."""
    if not TARGET_CHANNEL_ID or not isinstance(message_data, dict):
        return

    sensitive_keys = {
        "access_token", "authorization", "cookie", "cookies",
        "sessionid", "session_id", "password", "csrf_token",
    }

    def sanitize(value: Any, key: str = "") -> Any:
        if key.lower() in sensitive_keys:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): sanitize(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitize(v, key) for v in value]
        return value

    def collect_urls(value: Any, out: list[str]) -> None:
        if isinstance(value, str):
            out.extend(re.findall(r"https?://[^\\s\"']+", value))
        elif isinstance(value, dict):
            for k, v in value.items():
                collect_urls(v, out)
        elif isinstance(value, list):
            for v in value:
                collect_urls(v, out)

    try:
        _send_instagram_dm_inspection(
            bot,
            message_data,
            message_id=message_id,
        )

        urls: list[str] = []
        collect_urls(message_data, urls)
        urls = list(dict.fromkeys(urls))
        report = {
            "message_id": message_id,
            "instagram_canonical_urls": [
                u for u in urls
                if "instagram.com/" in u.lower()
                and re.search(r"/(?:p|reel|tv)/", u, re.I)
            ],
            "all_url_candidates": urls[:100],
            "message_data": sanitize(message_data),
        }

        temp_dir = Path(tempfile.mkdtemp(prefix="ig_inspection_"))
        try:
            safe_mid = re.sub(r"[^A-Za-z0-9_-]", "_", str(message_id or "unknown"))
            path = temp_dir / f"instagram_dm_inspection_{safe_mid}.json"
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            with path.open("rb") as fh:
                bot.send_document(
                    TARGET_CHANNEL_ID,
                    fh,
                    caption="Instagram DM inspection JSON — check instagram_canonical_urls.",
                )
            logger.info("Instagram DM inspection JSON sent to Telegram")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        logger.exception("Instagram DM inspection JSON failed")


def _download_and_forward(
    bot,
    attachments: list[dict[str, Any]],
    caption: str | None = None,
    message_id: str | None = None,
    sender_id: str | None = None,
    ig_user_id: str | None = None,
    message_data: dict[str, Any] | None = None,
) -> None:
    temp_dirs: list[str] = []
    all_files: list[Path] = []

    try:
        urls: list[str] = []
        direct_urls: set[str] = set()
        seen_ids: set[str] = set()

        # First recover the canonical URL from the actual Instagram DM.
        # Once found, the existing downloader can expand the complete
        # carousel exactly like a normal Instagram URL sent to Telegram.
        if sender_id:
            direct_share_urls = _instagram_direct_shared_post_urls(
                sender_id=sender_id,
                webhook_mid=message_id,
            )
            if direct_share_urls:
                logger.info(
                    "Instagram DM original-link resolver succeeded: %d URL(s)",
                    len(direct_share_urls),
                )
                for url in direct_share_urls:
                    try:
                        resolved_files, resolved_dir = download_public_url(url)
                        if resolved_files:
                            if resolved_dir:
                                temp_dirs.append(resolved_dir)
                            all_files.extend(resolved_files)
                            logger.info(
                                "Instagram DM original-link download succeeded: %d file(s)",
                                len(resolved_files),
                            )
                            break
                        if resolved_dir:
                            shutil.rmtree(resolved_dir, ignore_errors=True)
                    except Exception:
                        logger.exception(
                            "Instagram DM original-link download failed: %s",
                            url,
                        )
                if all_files:
                    _send_to_telegram(bot, all_files, caption=caption)
                    return

        # First inspect the complete webhook message object itself.
        # This path requires no cookie and no browser session. If Meta includes
        # a canonical shared-post URL anywhere in the payload, the normal
        # public downloader can expand the full carousel.
        webhook_links = _extract_instagram_links(message_data or {})
        if webhook_links:
            logger.info(
                "Instagram webhook payload contained %d canonical URL(s)",
                len(webhook_links),
            )
            for webhook_url in webhook_links:
                try:
                    resolved_files, resolved_dir = download_public_url(webhook_url)
                    if resolved_files:
                        if resolved_dir:
                            temp_dirs.append(resolved_dir)
                        all_files.extend(resolved_files)
                        logger.info(
                            "Instagram webhook canonical URL download succeeded: %d file(s)",
                            len(resolved_files),
                        )
                        break
                    if resolved_dir:
                        shutil.rmtree(resolved_dir, ignore_errors=True)
                except Exception:
                    logger.exception(
                        "Instagram webhook canonical URL download failed: %s",
                        webhook_url,
                    )
            if all_files:
                _send_to_telegram(bot, all_files, caption=caption)
                return

        # Optional browser-session resolver. It is deliberately a
        # separate feature switch so the normal downloader/resolver chain is
        # unchanged when INSTAGRAM_BROWSER_RESOLVER=false.
        if sender_id and INSTAGRAM_BROWSER_RESOLVER:
            browser_urls = _instagram_browser_shared_post_urls(
                sender_id=sender_id,
                webhook_mid=message_id,
            )
            if browser_urls:
                logger.info(
                    "Instagram browser resolver recovered %d canonical URL(s)",
                    len(browser_urls),
                )
                for browser_url in browser_urls:
                    try:
                        resolved_files, resolved_dir = download_public_url(browser_url)
                        if resolved_files:
                            if resolved_dir:
                                temp_dirs.append(resolved_dir)
                            all_files.extend(resolved_files)
                            logger.info(
                                "Instagram browser canonical URL download succeeded: %d file(s)",
                                len(resolved_files),
                            )
                            break
                        if resolved_dir:
                            shutil.rmtree(resolved_dir, ignore_errors=True)
                    except Exception:
                        logger.exception(
                            "Instagram browser canonical URL download failed: %s",
                            browser_url,
                        )
                if all_files:
                    _send_to_telegram(bot, all_files, caption=caption)
                    return

        # Meta's webhook can provide only a thin ig_post attachment.
        # Before falling back to the signed preview URL, ask the Graph
        # message/attachments endpoints for richer share metadata. This path
        # does not require an Instagram session cookie.
        if message_id and INSTAGRAM_ACCESS_TOKEN:
            try:
                recovered_attachments, recovered_links = _graph_message_details(
                    message_id=message_id,
                    ig_user_id=ig_user_id,
                    sender_id=sender_id,
                )
                if recovered_attachments:
                    logger.info(
                        "Instagram connector Graph fallback recovered %d attachment record(s)",
                        len(recovered_attachments),
                    )
                    attachments = recovered_attachments + attachments
                if recovered_links:
                    logger.info(
                        "Instagram connector Graph fallback recovered %d canonical URL(s)",
                        len(recovered_links),
                    )
                    for recovered_url in recovered_links:
                        try:
                            resolved_files, resolved_dir = download_public_url(recovered_url)
                            if resolved_files:
                                if resolved_dir:
                                    temp_dirs.append(resolved_dir)
                                all_files.extend(resolved_files)
                                logger.info(
                                    "Instagram connector Graph canonical URL download succeeded: %d file(s)",
                                    len(resolved_files),
                                )
                                break
                            if resolved_dir:
                                shutil.rmtree(resolved_dir, ignore_errors=True)
                        except Exception:
                            logger.exception(
                                "Instagram connector Graph canonical URL download failed: %s",
                                recovered_url,
                            )
                    if all_files:
                        _send_to_telegram(bot, all_files, caption=caption)
                        return
            except Exception:
                logger.exception("Instagram connector Graph message fallback failed")

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
            # Primary resolver: authenticated Instagram private/mobile API.
            # It can expose all resources of a carousel from the media PK.
            if isinstance(media_id, str):
                private_urls = _instagram_private_api_media_urls(media_id)
                if private_urls:
                    urls.extend(private_urls)
                    direct_urls.update(private_urls)
                    logger.info(
                        "Instagram share resolved through private API: %d item(s)",
                        len(private_urls),
                    )
                    continue

            # Secondary resolver: authenticated Instagram web session + Instaloader.
            if isinstance(media_id, str):
                instaloader_urls = _instagram_instaloader_media_urls(media_id)
                if instaloader_urls:
                    urls.extend(instaloader_urls)
                    direct_urls.update(instaloader_urls)
                    logger.info(
                        "Instagram share resolved through Instaloader: %d item(s)",
                        len(instaloader_urls),
                    )
                    continue

            # Tertiary resolver: Instagram's own mobile media-info API.
            if isinstance(media_id, str):
                mobile_urls = _instagram_mobile_media_urls(media_id)
                if mobile_urls:
                    urls.extend(mobile_urls)
                    direct_urls.update(mobile_urls)
                    logger.info(
                        "Instagram share resolved through mobile media-info: %d item(s)",
                        len(mobile_urls),
                    )
                    continue


            logger.info(
                "Instagram attachment: type=%s media_id_present=%s post_link_present=%s",
                attachment.get("type", "unknown"),
                bool(media_id),
                bool(post_link),
            )

            if isinstance(media_id, str) and media_id not in seen_ids:
                seen_ids.add(media_id)
                logger.info(
                    "Instagram share media_id=%s; Graph children lookup disabled",
                    media_id,
                )

                # If Meta does not expose the original permalink and the
                # mobile media-info endpoint rejects the request, derive the
                # public Instagram shortcode from the media ID and let
                # gallery-dl/yt-dlp resolve the public post. This is the key
                # fallback for shared public carousels.
                shortcode = _media_id_to_shortcode(media_id)
                if shortcode:
                    derived_link = f"https://www.instagram.com/p/{shortcode}/"
                    try:
                        logger.info(
                            "Instagram trying derived public permalink: %s",
                            derived_link,
                        )
                        derived_files, derived_dir = download_public_url(derived_link)
                        if derived_files:
                            if derived_dir:
                                temp_dirs.append(derived_dir)
                            all_files.extend(derived_files)
                            logger.info(
                                "Instagram derived permalink downloaded %d file(s)",
                                len(derived_files),
                            )
                            continue
                        if derived_dir:
                            shutil.rmtree(derived_dir, ignore_errors=True)
                        logger.info("Instagram derived permalink returned no files")
                    except Exception:
                        logger.exception("Instagram derived permalink fallback failed")

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

            # Try resolving the signed lookaside URL through an HTTP redirect.
            # If Meta redirects it to a public Instagram URL, gallery-dl can
            # expand the complete carousel without the media /children edge.
            media_url = payload.get("url")
            if isinstance(media_url, str) and media_url.startswith(("http://", "https://")):
                try:
                    redirect_response = requests.head(
                        media_url,
                        allow_redirects=True,
                        timeout=(15, 20),
                        headers={"User-Agent": "UltimateDownloader/1.0"},
                    )
                    final_url = redirect_response.url or ""
                    if "instagram.com/" in final_url and final_url != media_url:
                        public_url = final_url.split("?", 1)[0]
                        logger.info(
                            "Instagram CDN redirected to public URL: %s",
                            public_url,
                        )
                        permalink_files, permalink_dir = download_public_url(public_url)
                        if permalink_files:
                            if permalink_dir:
                                temp_dirs.append(permalink_dir)
                            all_files.extend(permalink_files)
                            logger.info(
                                "Instagram redirect fallback downloaded %d file(s)",
                                len(permalink_files),
                            )
                            continue
                        if permalink_dir:
                            shutil.rmtree(permalink_dir, ignore_errors=True)
                except Exception:
                    logger.debug(
                        "Instagram CDN redirect/public URL resolution failed",
                        exc_info=True,
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
    """Process one Instagram webhook entry through the private DM connector.

    The connector deliberately accepts only actual message events, ignores
    echo/self events, de-duplicates message IDs, and hands media resolution
    to the resolver chain above. It never exposes Instagram credentials.
    """
    if not isinstance(event, dict):
        return

    for item in event.get("messaging") or []:
        if not isinstance(item, dict):
            continue

        message = item.get("message") or {}
        if not isinstance(message, dict):
            continue

        if message.get("is_echo"):
            logger.info("Instagram connector ignored echo/self message")
            continue

        mid = str(message.get("mid") or "").strip()
        if _already_seen(mid):
            logger.info("Instagram connector ignored duplicate mid=%s", mid)
            continue

        attachments = message.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []

        sender = item.get("sender") or {}
        sender_id = str(sender.get("id") or "").strip() or None
        caption = message.get("text")
        caption = caption.strip() if isinstance(caption, str) and caption.strip() else None

        if not attachments:
            logger.info(
                "Instagram connector ignored non-media message mid=%s sender=%s",
                mid,
                sender_id,
            )
            continue

        logger.info(
            "Instagram connector received media DM: mid=%s sender=%s attachments=%d",
            mid,
            sender_id,
            len(attachments),
        )

        ig_user_id = str(event.get("id") or "").strip() or None

        threading.Thread(
            target=_download_and_forward,
            args=(bot, attachments, caption, mid, sender_id, ig_user_id, message),
            daemon=True,
        ).start()


def _verify_meta_signature(raw_body: bytes) -> bool:
    """Verify Meta's X-Hub-Signature-256 when an app secret is configured."""
    if not INSTAGRAM_APP_SECRET:
        logger.warning(
            "INSTAGRAM_APP_SECRET is not configured; accepting webhook without signature verification"
        )
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        INSTAGRAM_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


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
    raw_body = request.get_data(cache=True)
    if not _verify_meta_signature(raw_body):
        logger.warning("Instagram connector rejected invalid Meta webhook signature")
        return jsonify({"ok": False, "error": "invalid signature"}), 403

    payload = request.get_json(silent=True) or {}
    bot = app.config.get("telegram_bot")

    if bot is None:
        return (
            jsonify(
                {"ok": False, "error": "Telegram bot not initialized"}
            ),
            503,
        )

    entries = payload.get("entry") or []
    logger.info(
        "Instagram connector webhook received: object=%s entries=%d",
        payload.get("object"),
        len(entries) if isinstance(entries, list) else 0,
    )

    for entry in entries:
        if isinstance(entry, dict):
            _handle_event(bot, entry)

    # Meta expects a fast 200 acknowledgement; actual resolution/upload runs
    # asynchronously in a worker thread.
    return jsonify({"ok": True}), 200


@app.post("/telegram/webhook")
def receive_telegram_webhook():
    """Receive Telegram updates over HTTPS instead of getUpdates polling."""
    bot = app.config.get("telegram_bot")
    if bot is None:
        return jsonify({"ok": False, "error": "Telegram bot not initialized"}), 503

    if TELEGRAM_WEBHOOK_SECRET:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided != TELEGRAM_WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "Forbidden"}), 403

    try:
        raw = request.data.decode("utf-8")
        logger.info(
            "Telegram webhook received: bytes=%d update_id=%s",
            len(raw),
            (request.get_json(silent=True) or {}).get("update_id"),
        )
        update = tg_types.Update.de_json(raw)
        if update is None:
            logger.warning("Telegram webhook received an empty/unparseable update")
            return jsonify({"ok": False, "error": "invalid update"}), 400

        logger.info(
            "Telegram update parsed: update_id=%s has_message=%s text=%r",
            getattr(update, "update_id", None),
            bool(getattr(update, "message", None)),
            getattr(getattr(update, "message", None), "text", None),
        )
        bot.process_new_updates([update])
        logger.info("Telegram update dispatched successfully: update_id=%s", getattr(update, "update_id", None))
        return jsonify({"ok": True}), 200
    except Exception:
        logger.exception("Telegram webhook update processing failed")
        return jsonify({"ok": False}), 500


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

    if INSTAGRAM_BROWSER_RESOLVER:
        logger.info(
            "Instagram browser resolver: enabled; Playwright will inspect Direct web-session data"
        )
    else:
        logger.info(
            "Instagram browser resolver: disabled (set INSTAGRAM_BROWSER_RESOLVER=true to enable)"
        )

    if not INSTAGRAM_ACCESS_TOKEN:
        logger.warning(
            "INSTAGRAM_ACCESS_TOKEN is not configured; carousel expansion "
            "will fall back to the webhook's single media URL."
        )
    else:
        _instagram_token_ok()

    app.config["telegram_bot"] = bot

    # This module is an Instagram-only connector. Telegram remains on the
    # downloader's normal polling path in main.py; we intentionally do not
    # call set_webhook() here because Telegram polling and webhooks conflict.
    if INSTAGRAM_APP_SECRET:
        logger.info("Instagram connector Meta signature verification: enabled")
    else:
        logger.warning(
            "Instagram connector Meta signature verification: disabled "
            "(set INSTAGRAM_APP_SECRET for production hardening)"
        )

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
