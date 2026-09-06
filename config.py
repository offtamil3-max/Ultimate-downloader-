import os


class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    # Public Cobalt is a fallback, not the primary YouTube extractor.
    COBALT_ENDPOINTS: list[str] = ["https://api.cobalt.tools/"]
    MAX_MEDIA_GROUP: int = 10
    DOWNLOAD_ROOT: str = os.getenv("DOWNLOAD_ROOT", "downloads")
    TOOL_TIMEOUT: int = int(os.getenv("TOOL_TIMEOUT", "120"))
    COBALT_TIMEOUT: int = int(os.getenv("COBALT_TIMEOUT", "30"))
    MEDIA_TIMEOUT: int = int(os.getenv("MEDIA_TIMEOUT", "60"))
    # Telegram cloud Bot API uploads are capped at 50 MB; leave headroom.
    MAX_UPLOAD_BYTES: int = int(os.getenv("TELEGRAM_MAX_UPLOAD_BYTES", str(49 * 1024 * 1024)))

    @classmethod
    def validate(cls) -> None:
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is not set. Export it before starting the bot.")
