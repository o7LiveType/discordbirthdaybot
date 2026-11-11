import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def __init__(self, token=""):
        super().__init__()
        self.token = token

    def format(self, record):
        value = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            value["exception"] = self.formatException(record.exc_info)
        text = json.dumps(value, ensure_ascii=True)
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        return re.sub(r"(Bot |Bearer )[A-Za-z0-9._-]+", r"\1[REDACTED]", text)


def configure(directory: Path, level: str, token: str):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    formatter = JsonFormatter(token)
    console = logging.StreamHandler()
    file = RotatingFileHandler(directory / "bot.log", maxBytes=5_000_000, backupCount=5)
    for handler in (console, file):
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[console, file], force=True)
    # HTTP DEBUG can contain interaction payloads, dates, and credentials.
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
