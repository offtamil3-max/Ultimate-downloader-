"""Runtime compatibility patch for Instagram shared-post carousel resolution.

This file is loaded by Python startup and patches the webhook resolver without
requiring a fragile edit of the large webhook module. It adds the current
Instagram web JSON/embed fallbacks before the existing mobile/API fallbacks.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

logger = logging.getLogger("instagram_webhook")


def _extract_urls(value):
    urls = []

    def add(value):
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            if "instagram.com" in value or "cdninstagram.com" in value or "fbcdn.net" in value:
                urls.append(value)

    def walk(obj):
        if isinstance(obj, dict):
            # Current/legacy web GraphQL sidecar shape.
            sidecar = obj.get("edge_sidecar_to_children")
            if isinstance(sidecar, dict):
                for edge in sidecar.get("edges") or []:
                    if isinstance(edge, dict):
                        walk(edge.get("node") or {})

            # Newer/private JSON shape.
            carousel = obj.get("carousel_media")
            if isinstance(carousel, list):
                for item in carousel:
                    walk(item)

            videos = obj.get("video_versions")
            if isinstance(videos, list):
                candidates = [
                    x.get("url") for x in videos
                    if isinstance(x, dict) and isinstance(x.get("url"), str)
                ]
                if candidates:
                    add(candidates[-1])

            image_versions = obj.get("image_versions2")
            if isinstance(image_versions, dict):
                candidates = image_versions.get("candidates") or []
                if candidates:
                    best = max(
                        (x for x in candidates if isinstance(x, dict)),
                        key=lambda x: (x.get("width") or 0) * (x.get("height") or 0),
                        default=None,
                    )
                    if best:
                        add(best.get("url"))

            resources = obj.get("display_resources")
            if isinstance(resources, list) and resources:
                best = max(
                    (x for x in resources if isinstance(x, dict)),
                    key=lambda x: (x.get("config_width") or x.get("width") or 0)
                    * (x.get("config_height") or x.get("height") or 0),
                    default=None,
                )
                if best:
                    add(best.get("src"))

            # Legacy fields.
            add(obj.get("video_url"))
            add(obj.get("display_url"))

            for key in ("graphql", "data", "xdt_shortcode_media", "shortcode_media", "items"):
                if key in obj:
                    walk(obj.get(key))

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(value)
    return list(dict.fromkeys(urls))


def _media_from_json(data):
    candidates = [
        (data.get("graphql") or {}).get("shortcode_media"),
        (data.get("data") or {}).get("shortcode_media"),
        (data.get("data") or {}).get("xdt_shortcode_media"),
        (data.get("xdt_shortcode_media") if isinstance(data, dict) else None),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            urls = _extract_urls(candidate)
            if urls:
                return urls
    return _extract_urls(data)


def _patched_media_urls(media_id: str):
    try:
        module = __import__("instagram_webhook")
        shortcode_fn = getattr(module, "_media_id_to_shortcode", None)
        if not callable(shortcode_fn):
            return []

        shortcode = shortcode_fn(media_id)
        if not shortcode:
            return []

        cookie = ""
        cookie_fn = getattr(module, "_instagram_cookie_header", None)
        if callable(cookie_fn):
            cookie = cookie_fn()

        base_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
            "X-Requested-With": "XMLHttpRequest",
            "X-IG-App-ID": "936619743392459",
        }
        if cookie:
            base_headers["Cookie"] = cookie

        # Current web JSON fallback. Instagram has changed the response
        # envelope several times, so parse both legacy and newer shapes.
        json_url = f"https://www.instagram.com/p/{shortcode}/"
        for params in (
            {"__a": "1", "__d": "dis"},
            {"__a": "1"},
        ):
            try:
                response = requests.get(
                    json_url,
                    params=params,
                    headers={**base_headers, "Accept": "application/json,text/plain,*/*"},
                    timeout=(15, 30),
                    allow_redirects=True,
                )
                if response.ok:
                    try:
                        data = response.json()
                    except Exception:
                        data = json.loads(response.text)
                    urls = _media_from_json(data)
                    if urls:
                        logger.info(
                            "Instagram web JSON resolved %d media URL(s), shortcode=%s cookie_present=%s",
                            len(urls), shortcode, bool(cookie),
                        )
                        return urls
                else:
                    logger.info(
                        "Instagram web JSON lookup failed HTTP %s shortcode=%s cookie_present=%s",
                        response.status_code, shortcode, bool(cookie),
                    )
            except Exception:
                logger.debug("Instagram web JSON lookup errored", exc_info=True)

        # Embed fallback. This is useful when the normal post JSON endpoint
        # returns an HTML shell but includes additionalDataLoaded/static JSON.
        embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
        try:
            response = requests.get(
                embed_url,
                headers=base_headers,
                timeout=(15, 30),
                allow_redirects=True,
            )
            if response.ok:
                html = response.text
                blobs = re.findall(
                    r"window\.__additionalDataLoaded\([^,]+,(\{.*?\})\);",
                    html,
                    flags=re.S,
                )
                for blob in blobs:
                    try:
                        data = json.loads(blob)
                    except Exception:
                        continue
                    urls = _media_from_json(data)
                    if urls:
                        logger.info(
                            "Instagram embed JSON resolved %d media URL(s), shortcode=%s cookie_present=%s",
                            len(urls), shortcode, bool(cookie),
                        )
                        return urls

                # Some responses embed xdt_shortcode_media directly in script
                # JSON. Decode complete script-like JSON objects opportunistically.
                for match in re.findall(
                    r'("xdt_shortcode_media"\s*:\s*\{.*?\})(?:[,}])',
                    html,
                    flags=re.S,
                ):
                    try:
                        data = json.loads("{" + match + "}")
                    except Exception:
                        continue
                    urls = _media_from_json(data)
                    if urls:
                        logger.info(
                            "Instagram embed xdt data resolved %d media URL(s), shortcode=%s",
                            len(urls), shortcode,
                        )
                        return urls
        except Exception:
            logger.debug("Instagram embed lookup errored", exc_info=True)

    except Exception:
        logger.exception("Instagram web carousel fallback errored")

    return []


try:
    _mod = __import__("instagram_webhook")
    _original = getattr(_mod, "_instagram_mobile_media_urls", None)

    def _combined_media_urls(media_id: str):
        urls = _patched_media_urls(media_id)
        if urls:
            return urls
        if callable(_original):
            return _original(media_id)
        return []

    _mod._instagram_mobile_media_urls = _combined_media_urls
    logger.info("Instagram web carousel compatibility patch loaded")
except Exception:
    logger.debug("Instagram web carousel compatibility patch not loaded", exc_info=True)
