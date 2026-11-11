import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

DEFAULT_GIF = "https://static2.klipy.com/ii/d7aec6f6f171607374b2065c836f92f4/b5/ef/vrhOtJVs.gif"


def gif_url(value: str) -> str:
    parsed = urlparse(value.strip())
    allowed = {
        "static.klipy.com",
        "static2.klipy.com",
        "media.klipy.com",
        "i.klipy.com",
        "media.giphy.com",
        "i.giphy.com",
    }
    host = parsed.hostname or ""
    # Only media hosts. Never fetch arbitrary admin-supplied URLs server-side.
    if (
        parsed.scheme != "https"
        or host not in allowed
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.lower().endswith(".gif")
    ):
        raise ValueError(
            "Use a direct HTTPS .gif URL from static.klipy.com or a supported GIF media host (see README)."
        )
    return value.strip()


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    database: Path = Path("data/birthdays.sqlite3")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    test_guild_id: int | None = None
    gif: str = DEFAULT_GIF
    poll_seconds: int = 30

    @classmethod
    def load(cls):
        load_dotenv(override=False)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "replace-me":  # noqa: S105 - example placeholder, never a credential
            raise ValueError(
                "DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token."
            )
        guild = os.getenv("TEST_GUILD_ID", "").strip()
        gif = os.getenv("BIRTHDAY_GIF_URL", DEFAULT_GIF).strip() or DEFAULT_GIF
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL.")
        return cls(
            token,
            Path(os.getenv("DATABASE_PATH", "data/birthdays.sqlite3")),
            Path(os.getenv("LOG_DIR", "logs")),
            level,
            int(guild) if guild else None,
            gif_url(gif) if gif else "",
        )
