"""Universal public-URL fallback for Ultimate Downloader.

This module intentionally leaves the existing Instagram/X implementation alone.
It provides a generic extractor path for additional public sites supported by the
installed yt-dlp/gallery-dl packages.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "downloads"))
TOOL_TIMEOUT = int(os.getenv("TOOL_TIMEOUT", "120"))
COOKIES_FROM_BROWSER = os.getenv("COOKIES_FROM_BROWSER", "")
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")


def extract_http_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,!?;:)]}")


def _cookie_args() -> list[str]:
    args: list[str] = []
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        args += ["--cookies", COOKIES_FILE]
    elif COOKIES_FROM_BROWSER:
        args += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    return args


def _find_files(folder: Path) -> list[Path]:
    return sorted(
        [p for p in folder.rglob("*") if p.is_file() and p.stat().st_size > 0],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )


def _run_gallery_dl(url: str, folder: Path) -> list[Path]:
    command = ["gallery-dl", "--dest", str(folder), "--no-mtime"]
    command += _cookie_args()
    command.append(url)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TOOL_TIMEOUT)
        if result.returncode == 0:
            return _find_files(folder)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return []


def _run_yt_dlp(url: str, folder: Path) -> list[Path]:
    output = str(folder / "%(title).120s [%(id)s].%(ext)s")
    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-progress",
        "--restrict-filenames",
        "-o",
        output,
    ]
    command += _cookie_args()
    command.append(url)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TOOL_TIMEOUT)
        if result.returncode == 0:
            return _find_files(folder)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return []


def download_public_url(url: str) -> tuple[list[Path], Path]:
    """Try gallery-dl then yt-dlp for a public URL.

    Returns downloaded files and their temporary working directory. The caller
    owns cleanup of the returned directory.
    """
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="universal_", dir=DOWNLOAD_ROOT))

    files = _run_gallery_dl(url, folder)
    if not files:
        files = _run_yt_dlp(url, folder)

    if not files:
        shutil.rmtree(folder, ignore_errors=True)
        return [], folder
    return files, folder
