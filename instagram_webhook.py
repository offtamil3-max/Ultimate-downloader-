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
INSTAGRAM_SESSION_COOKIE = os.getenv("INSTAGRAM_SESSION_COOKIE", "").strip()
INSTAGRAM_CSRF_TOKEN = os.getenv("INSTAGRAM_CSRF_TOKEN", "").strip()
INSTAGRAM_WWW_CLAIM = os.getenv("INSTAGRAM_WWW_CLAIM", "").strip()
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
    """Build an Instagram cookie header from an explicit session cookie or cookies.txt."""
    if INSTAGRAM_SESSION_COOKIE:
        return INSTAGRAM_SESSION_COOKIE

    if not COOKIES_FILE:
        return ""

    try:
        from http.cookiejar import MozillaCookieJar

        jar = MozillaCookieJar(COOKIES_FILE)
        jar.load(ignore_discard=True, ignore_expires=True)
        pairs = []
        for cookie in jar:
            domain = (cookie.domain or "").lower()
            if "instagram.com" in domain:
                pairs.append(f"{cookie.name}={cookie.value}")
        return "; ".join(pairs)
    except Exception:
        logger.debug("Could not load Instagram cookies from %s", COOKIES_FILE, exc_info=True)
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
    if InstaGrapiClient is None or not sender_id:
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
                    or client.direct_messages(thread.id, amount=15)
                    or []
                )
            except Exception:
                continue

            exact = []
            recent_sender = []
            for dm in messages:
                dm_id = str(getattr(dm, "id", "") or "")
                dm_user_id = str(getattr(dm, "user_id", "") or "")
                urls = message_urls(dm)
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

        logger.info(
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

            data = response.json()
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


def _download_and_forward(
    bot,
    attachments: list[dict[str, Any]],
    caption: str | None = None,
    message_id: str | None = None,
    sender_id: str | None = None,
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
                args=(
                    bot,
                    attachments,
                    None,
                    mid,
                    (item.get("sender") or {}).get("id")
                    if isinstance(item.get("sender"), dict)
                    else None,
                ),
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
                    args=(bot, text_attachments, None, mid, None),
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

    if not INSTAGRAM_ACCESS_TOKEN:
        logger.warning(
            "INSTAGRAM_ACCESS_TOKEN is not configured; carousel expansion "
            "will fall back to the webhook's single media URL."
        )
    else:
        _instagram_token_ok()

    app.config["telegram_bot"] = bot

    # Telegram webhooks and getUpdates are mutually exclusive. Using the
    # webhook removes the recurring 409 conflict when another instance is
    # still polling the same bot token.
    telegram_webhook_url = f"{PUBLIC_BASE_URL}/telegram/webhook"
    try:
        if TELEGRAM_WEBHOOK_SECRET:
            bot.set_webhook(
                url=telegram_webhook_url,
                secret_token=TELEGRAM_WEBHOOK_SECRET,
                drop_pending_updates=False,
            )
        else:
            bot.set_webhook(
                url=telegram_webhook_url,
                drop_pending_updates=False,
            )
        logger.info("Telegram webhook configured: %s", telegram_webhook_url)
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
