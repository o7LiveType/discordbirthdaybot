import asyncio
from datetime import UTC, datetime

import pytest

from birthdaybot.database import Database


async def save(db, guild=1, user=2):
    await db.save(guild, user, 2, 29, "America/New_York", 9, 0, "feb28", False)


async def test_durability_modes_and_foreign_keys(db):
    assert (await db.read("PRAGMA journal_mode"))[0][0] == "wal"
    assert (await db.read("PRAGMA synchronous"))[0][0] == 2
    assert (await db.read("PRAGMA foreign_keys"))[0][0] == 1
    assert (await db.read("PRAGMA integrity_check"))[0][0] == "ok"


async def test_own_records_and_guild_isolation(db):
    await save(db)
    assert await db.birthday(1, 3) is None
    assert await db.birthday(3, 2) is None
    assert (await db.birthday(1, 2))["day"] == 29


async def test_claim_is_unique_under_contention_and_edits(db):
    await save(db)
    value = await db.birthday(1, 2)
    jobs = await asyncio.gather(*(db.claim(value, 2026, 4, 100) for _ in range(20)))
    assert len({job["marker"] for job in jobs}) == 1
    await db.delivery_state(jobs[0], "sent", message_id=99)
    await save(db)
    assert (await db.claim(value, 2026, 4, 100))["state"] == "sent"


async def test_delete_cascades_and_reopen_persists(db, tmp_path):
    await save(db)
    record = await db.birthday(1, 2)
    await db.claim(record, 2026, 4, 100)
    second = Database(db.path)
    await second.open()
    assert (await second.birthday(1, 2))["day"] == 29
    await second.close()
    await db.forget(1, 2)
    assert not await db.read("SELECT * FROM deliveries")


async def test_cancelled_transaction_rolls_back(db):
    with pytest.raises(asyncio.CancelledError):
        async with db.transaction() as conn:
            await conn.execute("UPDATE guilds SET enabled=0 WHERE guild_id=1")
            raise asyncio.CancelledError
    assert (await db.guild(1))["enabled"] == 1


async def test_notice_is_throttled_durably(db):
    now = datetime.now(UTC).timestamp()
    assert await db.notice_due(1, "broken", now)
    assert not await db.notice_due(1, "broken", now + 10)
    assert await db.notice_due(1, "broken", now + 3601)


async def test_guild_removal_erases_records_and_notices(db):
    await save(db)
    await db.notice_due(1, "x", 100)
    await db.drop_guild(1)
    assert not await db.read("SELECT * FROM birthdays")
    assert not await db.read("SELECT * FROM notices")


async def test_delete_and_readd_cannot_bypass_annual_spam_guard(db):
    await save(db)
    job = await db.claim(await db.birthday(1, 2), 2026, 100, 100)
    await db.delivery_state(job, "sent")
    await db.forget(1, 2)
    await db.save(1, 2, 11, 10, "America/New_York", 9, 0, "feb28", True)
    assert not await db.candidates(datetime(2026, 11, 10, 17, tzinfo=UTC))
    assert len(await db.candidates(datetime(2027, 11, 10, 17, tzinfo=UTC))) == 1


async def test_candidate_filter_handles_date_line_leap_day_and_private_records(db):
    await db.save(1, 2, 1, 1, "Pacific/Kiritimati", 0, 0, "feb28", True)
    assert len(await db.candidates(datetime(2025, 12, 31, 10, tzinfo=UTC))) == 1
    job = await db.claim(await db.birthday(1, 2), 2026, 100, 100)
    await db.delivery_state(job, "sent")
    assert not await db.candidates(datetime(2025, 12, 31, 10, tzinfo=UTC))
    await db.save(1, 3, 2, 29, "UTC", 0, 0, "mar1", True)
    await db.save(1, 4, 3, 1, "UTC", 0, 0, "feb28", False)
    assert [r["user_id"] for r in await db.candidates(datetime(2027, 3, 1, 9, tzinfo=UTC))] == [3]


async def test_admin_time_change_applies_to_existing_records(db):
    await save(db)
    await db.configure(1, 100, None, "UTC", 10, 30, "", "mar1")
    value = await db.birthday(1, 2)
    assert (value["hour"], value["minute"], value["leap"]) == (10, 30, "mar1")


async def test_new_servers_default_to_midnight(db):
    assert ((await db.guild(1))["hour"], (await db.guild(1))["minute"]) == (0, 0)
    assert (await db.guild(1))["timezone"] == "UTC"


@pytest.mark.parametrize(
    ("legacy_version", "legacy_zone", "member_zone"),
    [(5, "America/Denver", "Asia/Tokyo"), (6, "America/New_York", "America/New_York")],
)
async def test_utc_prefill_migration_preserves_explicit_member_timezones(
    db, legacy_version, legacy_zone, member_zone
):
    await db.ensure_guild(2)
    await db.configure(1, 100, None, legacy_zone, 0, 0, "")
    await db.configure(2, 200, None, "Europe/London", 0, 0, "")
    await db.save(1, 10, 1, 1, member_zone, 0, 0, "feb28", True)
    async with db.transaction() as conn:
        await conn.execute(f"PRAGMA user_version={legacy_version}")  # noqa: S608 - test integer versions
    upgraded = Database(db.path)
    await upgraded.open()
    try:
        assert (await upgraded.guild(1))["timezone"] == "UTC"
        assert (await upgraded.guild(2))["timezone"] == "Europe/London"
        assert (await upgraded.birthday(1, 10))["timezone"] == member_zone
        await upgraded.ensure_guild(3)
        assert (await upgraded.guild(3))["timezone"] == "UTC"
    finally:
        await upgraded.close()


async def test_midnight_upgrade_updates_legacy_records_once_and_preserves_custom_times(db):
    await db.ensure_guild(2)
    await db.configure(1, 100, None, "UTC", 9, 0, "")
    await db.configure(2, 200, None, "UTC", 18, 45, "")
    await db.save(1, 10, 1, 1, "America/New_York", 9, 0, "feb28", True)
    await db.save(2, 20, 1, 1, "Asia/Tokyo", 18, 45, "feb28", False)
    async with db.transaction() as conn:
        await conn.execute("PRAGMA user_version=4")
    upgraded = Database(db.path)
    await upgraded.open()
    try:
        assert (await upgraded.guild(1))["hour"] == 0
        assert (await upgraded.birthday(1, 10))["hour"] == 0
        assert (await upgraded.guild(2))["hour"] == 18
        private = await upgraded.birthday(2, 20)
        assert (private["hour"], private["minute"], private["consent"]) == (18, 45, 0)
        await upgraded.configure(1, 100, None, "UTC", 9, 0, "")
    finally:
        await upgraded.close()
    reopened = Database(db.path)
    await reopened.open()
    try:
        assert (await reopened.guild(1))["hour"] == 9
        assert (await reopened.birthday(1, 10))["hour"] == 9
    finally:
        await reopened.close()
