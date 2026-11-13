"""Authoritative replacements must be atomic, server-scoped and spam-safe."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from test_delivery import NOW, fixture

from birthdaybot.csv_import import ImportRow, parse_csv
from birthdaybot.sharing import Sharing


async def test_csv_replaces_dates_flags_and_active_pending_records_and_removes_absent(db):
    await db.import_birthdays(
        1,
        50,
        [
            ImportRow(2, 10, 1, 1, "UTC", True),
            ImportRow(3, 20, 2, 2, None, True),
            ImportRow(4, 30, 3, 3, "UTC", True),
            ImportRow(5, 40, 4, 4, None, True),
        ],
    )
    await db.ensure_guild(2)
    await db.save(2, 30, 5, 5, "Asia/Tokyo", 0, 0, "feb28", True)
    await db.configure(1, 100, 101, "UTC", 18, 45, "", "mar1", False, True)
    settings = dict(await db.guild(1))
    plan = await db.import_plan(1, [10, 20, 50])
    assert (plan.added, plan.updated, plan.removed) == (1, 2, 2)
    rows = [
        ImportRow(2, 10, 8, 15, None, False),
        ImportRow(3, 20, 2, 29, "Europe/Bucharest", None, True, False, True, False),
        ImportRow(4, 50, 6, 12, "Asia/Kathmandu", True),
    ]
    result = await db.import_birthdays(1, 99, rows)
    assert result == (2, 1, 1, 2, 2)
    assert await db.birthday(1, 10) is None
    pending = await db.pending_birthday(1, 10)
    assert (pending["month"], pending["day"], pending["imported_by"]) == (8, 15, 99)
    assert Sharing.from_record(pending) == Sharing.all(False)
    active = await db.birthday(1, 20)
    assert (
        active["month"],
        active["day"],
        active["timezone"],
        active["hour"],
        active["minute"],
        active["leap"],
    ) == (2, 29, "Europe/Bucharest", 18, 45, "mar1")
    assert Sharing.from_record(active) == Sharing(True, False, True, False)
    assert await db.pending_birthday(1, 20) is None
    assert await db.birthday(1, 30) is None
    assert await db.pending_birthday(1, 40) is None
    assert dict(await db.guild(1)) == settings
    assert (await db.birthday(2, 30))["month"] == 5
    assert (await db.read("PRAGMA integrity_check"))[0][0] == "ok"
    assert not await db.read("PRAGMA foreign_key_check")


async def test_failed_replacement_rolls_back_old_records_jobs_and_directory(db, monkeypatch):
    await db.save(1, 10, 1, 1, "UTC", 0, 0, "feb28", True)
    await db.register_directory(1, 100, 500, NOW.timestamp())
    job = await db.claim(await db.birthday(1, 10), 2026, 100, NOW.timestamp())
    original = dict(await db.birthday(1, 10))
    monkeypatch.setattr(
        db, "_invalidate_directories", AsyncMock(side_effect=RuntimeError("forced failure"))
    )
    with pytest.raises(RuntimeError):
        await db.import_birthdays(1, 20, [ImportRow(2, 20, 2, 2, "UTC", False)])
    assert dict(await db.birthday(1, 10)) == original
    assert await db.birthday(1, 20) is None
    assert (await db.read("SELECT marker FROM deliveries"))[0][0] == job["marker"]
    assert (await db.read("SELECT message_id FROM directory_messages"))[0][0] == 500


async def test_invalid_replacement_preserves_existing_data_before_any_writes(db):
    original = ImportRow(2, 10, 1, 1, "UTC", True)
    await db.import_birthdays(1, 20, [original])
    with pytest.raises(ValueError):
        await db.import_birthdays(1, 20, [replace(original, month=4, day=31)])
    assert (await db.birthday(1, 10))["month"] == 1


async def test_reimports_and_removal_readdition_never_repeat_sent_birthday_greetings(db):
    scheduler, guild, channel = await fixture(db)
    await scheduler.tick(NOW)
    row = ImportRow(2, 2, 11, 10, "America/New_York", True)
    await db.import_birthdays(1, 20, [row])
    await scheduler.tick(NOW)
    await db.import_birthdays(1, 20, [])
    await db.import_birthdays(1, 20, [row])
    await scheduler.tick(NOW)
    assert channel.send.await_count == 1
    assert guild.fetch_member.return_value.create_dm.return_value.send.await_count == 1
    assert len(await db.read("SELECT * FROM receipts")) == 1
    assert len(await db.read("SELECT * FROM cleanup")) == 2


async def test_replacement_preserves_ambiguous_jobs_receipts_and_cleanup_but_cancels_unsent(db):
    await db.save(1, 10, 1, 1, "UTC", 0, 0, "feb28", True)
    record = await db.birthday(1, 10)
    for index, state in enumerate(("ready", "retry", "sending", "uncertain", "sent", "skipped")):
        job = await db.claim(record, 2020 + index, 100, NOW.timestamp())
        await db.delivery_state(job, state)
        dm = await db.claim_dm(record, 2020 + index, NOW.timestamp())
        await db.dm_state(dm, state)
    blocked = await db.claim_dm(record, 2030, NOW.timestamp())
    await db.dm_state(blocked, "blocked")
    started = datetime.now(UTC).timestamp()
    await db.register_directory(1, 100, 500, started)
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO cleanup(message_id,guild_id,channel_id,delete_at) VALUES (600,1,100,9999999999)"
        )
    assert await db.import_birthdays(1, 20, []) == (0, 0, 0, 0, 1)
    assert {row["state"] for row in await db.read("SELECT state FROM deliveries")} == {
        "sending",
        "uncertain",
        "sent",
        "skipped",
    }
    assert {row["state"] for row in await db.read("SELECT state FROM dm_deliveries")} == {
        "sending",
        "uncertain",
        "sent",
        "skipped",
        "blocked",
    }
    assert len(await db.read("SELECT * FROM receipts")) == 1
    assert not await db.read("SELECT * FROM directory_messages")
    assert (await db.read("SELECT delete_at FROM cleanup WHERE message_id=600"))[0][0] == 9999999999
    assert (
        started
        <= (await db.read("SELECT delete_at FROM cleanup WHERE message_id=500"))[0][0]
        < started + 86400
    )


def test_header_only_csv_is_an_explicit_empty_replacement():
    parsed = parse_csv(b"user_id,month,day,timezone,opt_out\n")
    assert parsed.rows == () and parsed.issues == () and parsed.total == 0
