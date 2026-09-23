"""Playwright-based Instagram share resolver.

This module opens the derived Instagram post in a real Chromium browser using
the existing session cookie. It only resolves a canonical Instagram URL; the
existing downloader handles the actual media.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger("instagram_browser")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def _cookies(header: str) -> list[dict[str, str]]:
    result = []
    for part in (header or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            result.append({
                "name": name,
                "value": value.strip(),
                "domain": ".instagram.com",
                "path": "/",
            })
    return result


def _instagram_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().replace("\\/", "/")
    if not value.startswith(("https://www.instagram.com/", "https://instagram.com/")):
        return None
    parsed = urlparse(value)
    if parsed.path.startswith(("/p/", "/reel/", "/tv/")):
        return f"https://www.instagram.com{parsed.path}"
    return None


def _shortcode(media_id: str) -> str | None:
    try:
        value = int(media_id)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    chars = []
    while value:
        chars.append(alphabet[value & 63])
        value >>= 6
    return "".join(reversed(chars))


def resolve_media_id(media_id: str, session_cookie: str) -> list[str]:
    if sync_playwright is None:
        logger.warning("Instagram browser resolver skipped: Playwright unavailable")
        return []
    if not media_id or not media_id.isdigit() or not session_cookie:
        return []

    shortcode = _shortcode(media_id)
    if not shortcode:
        return []

    targets = [
        f"https://www.instagram.com/p/{shortcode}/",
        f"https://www.instagram.com/reel/{shortcode}/",
    ]
    found: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Mobile Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 412, "height": 915},
            )
            context.add_cookies(_cookies(session_cookie))

            for target in targets:
                page = None
                try:
                    page = context.new_page()
                    page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)

                    candidates = [page.url]
                    for selector in (
                        'link[rel="canonical"]',
                        'meta[property="og:url"]',
                        'meta[name="og:url"]',
                    ):
                        locator = page.locator(selector)
                        count = min(locator.count(), 5)
                        for i in range(count):
                            try:
                                value = (
                                    locator.nth(i).get_attribute("href")
                                    or locator.nth(i).get_attribute("content")
                                )
                                if value:
                                    candidates.append(value)
                            except Exception:
                                pass

                    try:
                        html = page.content()
                        candidates.extend(
                            re.findall(
                                r'https:\\/\\/(?:www\\.)?instagram\\.com\\/(?:p|reel|tv)\\/[A-Za-z0-9_-]+',
                                html,
                            )
                        )
                    except Exception:
                        pass

                    for candidate in candidates:
                        url = _instagram_url(candidate)
                        if url and url not in found:
                            found.append(url)

                    logger.info(
                        "Instagram browser resolver target=%s final=%s urls=%d",
                        target,
                        page.url,
                        len(found),
                    )

                    if found:
                        break
                except Exception:
                    logger.info(
                        "Instagram browser resolver target failed: %s",
                        target,
                        exc_info=True,
                    )
                finally:
                    if page:
                        try:
                            page.close()
                        except Exception:
                            pass

                if found:
                    break

            context.close()
            browser.close()
    except Exception:
        logger.exception("Instagram browser resolver failed for media_id=%s", media_id)

    if found:
        logger.info(
            "Instagram browser resolver recovered %d canonical URL(s), shortcode=%s",
            len(found),
            shortcode,
        )
    else:
        logger.warning(
            "Instagram browser resolver found no canonical URL, shortcode=%s",
            shortcode,
        )
    return found
