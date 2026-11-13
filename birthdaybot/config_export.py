"""Versioned, allowlisted CSV exports of saved server settings."""

import csv
import io
from datetime import UTC, datetime

from .config import gif_url
from .timeutil import timezone


def config_document(settings, default_gif):
    gif = settings["gif"] or default_gif
    document = {
        "format": "discordbirthdaybot.server-config",
        "version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "guild_id": str(settings["guild_id"]),
        "channel_id": str(settings["channel_id"]) if settings["channel_id"] else "",
        "alert_channel_id": str(settings["alert_channel_id"])
        if settings["alert_channel_id"]
        else "",
        "timezone": timezone(settings["timezone"]),
        "hour": settings["hour"],
        "minute": settings["minute"],
        "enabled": str(bool(settings["enabled"])).lower(),
        "gif": gif_url(gif) if gif else "",
        "leap": settings["leap"],
        "public_birthdays": str(bool(settings["public_birthdays"])).lower(),
        "ping_everyone": str(bool(settings["ping_everyone"])).lower(),
    }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(document), lineterminator="\n", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    writer.writerow(document)
    return output.getvalue().encode("utf-8")
