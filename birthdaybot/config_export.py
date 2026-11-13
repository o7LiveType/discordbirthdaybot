"""Versioned, allowlisted exports of saved server settings."""

import json
from datetime import UTC, datetime

from .config import gif_url


def config_document(settings, default_gif):
    gif = settings["gif"] or default_gif
    document = {
        "format": "discordbirthdaybot.server-config",
        "version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "guild_id": str(settings["guild_id"]),
        "settings": {
            "channel_id": str(settings["channel_id"]) if settings["channel_id"] else None,
            "alert_channel_id": str(settings["alert_channel_id"])
            if settings["alert_channel_id"]
            else None,
            "timezone": settings["timezone"],
            "hour": settings["hour"],
            "minute": settings["minute"],
            "enabled": bool(settings["enabled"]),
            "gif": gif_url(gif) if gif else "",
            "leap": settings["leap"],
            "public_birthdays": bool(settings["public_birthdays"]),
            "ping_everyone": bool(settings["ping_everyone"]),
        },
    }
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
