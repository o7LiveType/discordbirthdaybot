import csv
import io
from datetime import datetime

import pytest

from birthdaybot.config import DEFAULT_GIF
from birthdaybot.config_export import config_document


async def test_config_export_defaults_types_and_allowlist(db):
    settings = dict(await db.guild(1))
    settings["future_private_column"] = "must-never-be-exported"
    raw = config_document(settings, DEFAULT_GIF)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    assert len(rows) == 1
    document = rows[0]
    assert document["format"] == "discordbirthdaybot.server-config"
    assert document["version"] == "1"
    assert document["guild_id"] == "1"
    assert datetime.fromisoformat(document["exported_at"]).utcoffset().total_seconds() == 0
    assert {
        key: value
        for key, value in document.items()
        if key not in {"format", "version", "exported_at", "guild_id"}
    } == {
        "channel_id": "",
        "alert_channel_id": "",
        "timezone": "UTC",
        "hour": "0",
        "minute": "0",
        "enabled": "true",
        "gif": DEFAULT_GIF,
        "leap": "feb28",
        "public_birthdays": "true",
        "ping_everyone": "false",
    }
    assert b"must-never-be-exported" not in raw
    assert (await db.guild(1))["gif"] == ""  # Export does not save the resolved default.


async def test_config_csv_rejects_unsafe_timezone_cells(db):
    settings = dict(await db.guild(1))
    settings["timezone"] = '=HYPERLINK("https://invalid.example")'
    with pytest.raises(ValueError):
        config_document(settings, DEFAULT_GIF)
