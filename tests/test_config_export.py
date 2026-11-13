import json
from datetime import datetime

from birthdaybot.config import DEFAULT_GIF
from birthdaybot.config_export import config_document


async def test_config_export_defaults_types_and_allowlist(db):
    settings = dict(await db.guild(1))
    settings["future_private_column"] = "must-never-be-exported"
    raw = config_document(settings, DEFAULT_GIF)
    document = json.loads(raw)
    assert document["format"] == "discordbirthdaybot.server-config"
    assert document["version"] == 1
    assert document["guild_id"] == "1"
    assert datetime.fromisoformat(document["exported_at"]).utcoffset().total_seconds() == 0
    assert document["settings"] == {
        "channel_id": None,
        "alert_channel_id": None,
        "timezone": "UTC",
        "hour": 0,
        "minute": 0,
        "enabled": True,
        "gif": DEFAULT_GIF,
        "leap": "feb28",
        "public_birthdays": True,
        "ping_everyone": False,
    }
    assert b"must-never-be-exported" not in raw
    assert (await db.guild(1))["gif"] == ""  # Export does not save the resolved default.
