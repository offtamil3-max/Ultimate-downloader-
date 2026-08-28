import os
from typing import Optional

class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    # Optional Cobalt instances (public + self-hosted fallback)
    COBALT_ENDPOINTS: list[str] = [
        "https://api.cobalt.tools/",
        "https://co.wuk.sh/api/json",
    ]
    # Max items per Telegram media group
    MAX_MEDIA_GROUP: int = 10
    # Temporary download root
    DOWNLOAD_ROOT: str = "downloads"
    # Timeout for external tools (seconds)
    TOOL_TIMEOUT: int = 120

    @classmethod
    def validate(cls) -> None:
        if not cls.BOT_TOKEN:
            raise ValueError(
                "BOT_TOKEN environment variable is not set. "
                "Export it before starting the bot."
            )
