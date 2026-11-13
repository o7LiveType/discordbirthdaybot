import itertools
import sqlite3
from datetime import UTC, datetime

import pytest
from test_delivery import NOW, fixture

from birthdaybot.csv_import import ImportRow
from birthdaybot.database import SCHEMA, Database
from birthdaybot.sharing import FLAGS, Sharing


@pytest.mark.parametrize("values", list(itertools.product((False, True), repeat=4)))
async def test_permissions_independently_control_lists_posts_dms_and_mentions(db, values):
    scheduler, guild, channel = await fixture(db)
    member = guild.fetch_member.return_value
    member.display_name = "Example @everyone"
    flags = Sharing(*values)
    await db.set_sharing(1, 2, flags)
    assert bool((await db.directory(1))["visible"]) is flags.list_public
    assert bool(await db.candidates(NOW)) is flags.announce
    assert bool(await db.candidates(NOW, dm=True)) is flags.dm_enabled
    await scheduler.tick(NOW)
    assert channel.send.await_count == int(flags.announce)
    assert member.create_dm.return_value.send.await_count == int(flags.dm_enabled)
    if flags.announce:
        payload = channel.send.call_args.kwargs
        assert not payload["allowed_mentions"].everyone and not payload["allowed_mentions"].roles
        if flags.mention_enabled:
            assert [user.id for user in payload["allowed_mentions"].users] == [2]
            assert "<@2>" in payload["embed"].description
        else:
            assert payload["allowed_mentions"].users is False
            assert "<@2>" not in payload["embed"].description
            assert "@everyone" not in payload["content"]
            assert "@everyone" not in payload["embed"].description


async def test_listing_changes_invalidate_snapshots_but_other_flags_do_not(db):
    await db.save(1, 2, 1, 1, "UTC", 0, 0, "feb28", True)
    await db.register_directory(1, 100, 500, datetime.now(UTC).timestamp())
    await db.set_sharing(1, 2, Sharing(True, False, False, False))
    assert await db.directory_message(1, 100)
    await db.set_sharing(1, 2, Sharing(False, False, False, False))
    assert await db.directory_message(1, 100) is None
    assert len(await db.read("SELECT * FROM cleanup WHERE message_id=500")) == 1


async def test_preferences_only_update_preserves_dates_and_other_servers(db):
    await db.ensure_guild(2)
    for guild in (1, 2):
        await db.save(guild, 2, 2, 29, "Asia/Kathmandu", 18, 45, "mar1", True)
    flags = Sharing(False, True, False, False)
    await db.set_sharing(1, 2, flags)
    value = await db.birthday(1, 2)
    assert (
        value["month"],
        value["day"],
        value["timezone"],
        value["hour"],
        value["minute"],
        value["leap"],
    ) == (2, 29, "Asia/Kathmandu", 18, 45, "mar1")
    assert Sharing.from_record(value) == flags
    assert Sharing.from_record(await db.birthday(2, 2)) == Sharing.all(True)
    # Date edits made through the form retain every independent choice.
    await db.save(1, 2, 3, 1, "Europe/London", 18, 45, "mar1", flags.enabled, sharing=flags)
    assert Sharing.from_record(await db.birthday(1, 2)) == flags
    with pytest.raises(ValueError):
        await db.set_sharing(1, 3, flags)


async def test_pending_preferences_survive_claim_without_changing_import_provenance(db):
    member = 111111111111111111
    await db.import_birthdays(1, 20, [ImportRow(2, member, 1, 1, None, False)])
    original = await db.pending_birthday(1, member)
    flags = Sharing(True, False, True, False)
    await db.set_sharing(1, member, flags)
    pending = await db.pending_birthday(1, member)
    assert Sharing.from_record(pending) == flags
    assert (pending["imported_at"], pending["imported_by"]) == (
        original["imported_at"],
        original["imported_by"],
    )
    assert not (await db.directory(1))["visible"]
    await db.save(1, member, 1, 1, "UTC", 0, 0, "feb28", flags.enabled, sharing=flags)
    assert Sharing.from_record(await db.birthday(1, member)) == flags
    assert await db.pending_birthday(1, member) is None


async def test_changes_cannot_reset_sent_or_uncertain_annual_receipts(db):
    scheduler, guild, channel = await fixture(db)
    await scheduler.tick(NOW)
    dm = guild.fetch_member.return_value.create_dm.return_value
    await db.set_sharing(1, 2, Sharing.all(False))
    await db.set_sharing(1, 2, Sharing.all(True))
    await scheduler.tick(NOW)
    assert dm.send.await_count == channel.send.await_count == 1
    assert (await db.read("SELECT state FROM dm_deliveries"))[0][0] == "sent"
    await db.dm_state((await db.read("SELECT * FROM dm_deliveries"))[0], "uncertain")
    await db.set_sharing(1, 2, Sharing.all(False))
    assert (await db.read("SELECT state FROM dm_deliveries"))[0][0] == "uncertain"


async def test_queued_dm_is_cancelled_when_only_dm_permission_is_revoked(db):
    scheduler, guild, channel = await fixture(db)
    record = await db.birthday(1, 2)
    await db.claim_dm(record, 2026, NOW.timestamp())
    await db.set_sharing(1, 2, Sharing(True, True, False, True))
    await scheduler.tick(NOW)
    guild.fetch_member.return_value.create_dm.return_value.send.assert_not_awaited()
    channel.send.assert_awaited_once()


async def test_schema8_migration_preserves_on_off_and_pending_records(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy = "\n".join(
        line
        for line in SCHEMA.splitlines()
        if not any(f"{key} INTEGER" in line for key in FLAGS)
        and not (line.startswith("CREATE INDEX") and any(f"WHERE {key}=" in line for key in FLAGS))
    )
    legacy = legacy.replace("PRAGMA user_version=10;", "PRAGMA user_version=8;")
    with sqlite3.connect(path) as conn:
        conn.executescript(legacy)
        conn.execute("INSERT INTO guilds(guild_id) VALUES (1)")
        for member, enabled in ((10, 1), (20, 0)):
            conn.execute(
                "INSERT INTO birthdays VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, member, 2, 29, "UTC", 0, 0, "feb28", enabled, 100),
            )
            conn.execute(
                "INSERT INTO birthday_import_pending VALUES (?,?,?,?,?,?,?,?)",
                (1, member + 1, 3, 1, None, enabled, 100, 50),
            )
    db = Database(path)
    await db.open()
    try:
        for member, enabled in ((10, True), (20, False)):
            assert Sharing.from_record(await db.birthday(1, member)) == Sharing.all(enabled)
            assert Sharing.from_record(await db.pending_birthday(1, member + 1)) == Sharing.all(
                enabled
            )
        flags = Sharing(False, True, False, False)
        await db.set_sharing(1, 10, flags)
        await db.close()
        await db.open()
        assert Sharing.from_record(await db.birthday(1, 10)) == flags
        assert (await db.read("PRAGMA user_version"))[0][0] == 10
        assert (await db.read("PRAGMA integrity_check"))[0][0] == "ok"
        assert not await db.read("PRAGMA foreign_key_check")
    finally:
        await db.close()


async def test_unrelated_permission_edits_preserve_dm_retry_backoff(db):
    scheduler, guild, channel = await fixture(db)
    job = await db.claim_dm(await db.birthday(1, 2), 2026, NOW.timestamp())
    await db.dm_state(job, "retry", retry_at=NOW.timestamp() + 3600)
    await db.set_sharing(1, 2, Sharing(False, True, True, False))
    retained = await db.dm_job(1, 2, 2026)
    assert retained["marker"] == job["marker"]
    assert retained["retry_at"] == NOW.timestamp() + 3600


@pytest.mark.parametrize("invalid", ["false", "yes", None, 2, -1])
def test_invalid_legacy_sharing_cannot_silently_enable_permissions(invalid):
    with pytest.raises(ValueError):
        Sharing.all(invalid)
