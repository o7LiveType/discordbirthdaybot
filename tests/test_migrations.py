"""Upgrade the actual prior schema, with its latest data still in WAL."""

import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from birthdaybot.database import SCHEMA, SCHEMA_VERSION, Database
from birthdaybot.sharing import Sharing


def legacy_database(path):
    legacy = SCHEMA.replace(
        ",\n ping_everyone INTEGER NOT NULL DEFAULT 0 CHECK(ping_everyone IN (0,1))", ""
    ).replace(f"PRAGMA user_version={SCHEMA_VERSION};", "PRAGMA user_version=9;")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(legacy)
    conn.execute("INSERT INTO guilds(guild_id) VALUES (1)")
    conn.execute(
        "INSERT INTO birthdays VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, 20, 2, 29, "Asia/Kathmandu", 18, 45, "mar1", 1, 100, 0, 1, 0, 0),
    )
    conn.execute(
        "INSERT INTO birthday_import_pending VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, 30, 3, 1, None, 1, 100, 50, 1, 0, 1, 0),
    )
    conn.execute(
        "INSERT INTO deliveries(guild_id,user_id,year,state,marker,channel_id,created_at) VALUES (1,20,2026,'sent','marker',100,100)"
    )
    conn.execute("INSERT INTO receipts VALUES (1,20,2026)")
    conn.execute(
        "INSERT INTO cleanup(message_id,guild_id,channel_id,delete_at) VALUES (500,1,100,1000)"
    )
    conn.commit()
    return conn


async def test_schema9_upgrade_backs_up_wal_preserves_data_and_defaults_everyone_off(tmp_path):
    path = tmp_path / "birthdays.sqlite3"
    with closing(legacy_database(path)):
        assert path.with_suffix(".sqlite3-wal").stat().st_size > 0
        db = Database(path)
        await db.open()
        try:
            assert (await db.read("PRAGMA user_version"))[0][0] == SCHEMA_VERSION
            assert not (await db.guild(1))["ping_everyone"]
            assert Sharing.from_record(await db.birthday(1, 20)) == Sharing(
                False, True, False, False
            )
            assert Sharing.from_record(await db.pending_birthday(1, 30)) == Sharing(
                True, False, True, False
            )
            assert (await db.birthday(1, 20))["timezone"] == "Asia/Kathmandu"
            assert (await db.read("SELECT state FROM deliveries"))[0][0] == "sent"
            assert len(await db.read("SELECT * FROM receipts")) == 1
            assert len(await db.read("SELECT * FROM cleanup")) == 1
            assert (await db.read("PRAGMA integrity_check"))[0][0] == "ok"
            assert not await db.read("PRAGMA foreign_key_check")
            snapshots = list((tmp_path / "backups").glob("*.sqlite3"))
            assert len(snapshots) == 1
            snapshot = snapshots[0]
            if os.name != "nt":
                assert snapshot.stat().st_mode & 0o777 == 0o600
                assert snapshot.parent.stat().st_mode & 0o777 == 0o700
            with closing(sqlite3.connect(snapshot)) as backup:
                assert backup.execute("PRAGMA user_version").fetchone()[0] == 9
                assert "ping_everyone" not in {
                    r[1] for r in backup.execute("PRAGMA table_info(guilds)")
                }
                assert backup.execute("SELECT day,timezone FROM birthdays").fetchone() == (
                    29,
                    "Asia/Kathmandu",
                )
                assert (
                    backup.execute("SELECT count(*) FROM birthday_import_pending").fetchone()[0]
                    == 1
                )
                assert backup.execute("SELECT state FROM deliveries").fetchone()[0] == "sent"
                assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            await db.configure(1, 100, None, "UTC", 18, 45, "", ping_everyone=True)
        finally:
            await db.close()
    await db.open()
    try:
        assert (await db.guild(1))["ping_everyone"]
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
        # Other settings callers must preserve the separately configured ping.
        await db.configure(1, 100, None, "UTC", 12, 0, "")
        assert (await db.guild(1))["ping_everyone"]
    finally:
        await db.close()


async def test_unwritable_backup_directory_aborts_before_any_schema_changes(tmp_path):
    path = tmp_path / "birthdays.sqlite3"
    (tmp_path / "backups").write_text("Existing file must not be overwritten.")
    with closing(legacy_database(path)) as source:
        with pytest.raises(RuntimeError, match="backup failed; schema upgrade was not started"):
            await Database(path).open()
        assert source.execute("PRAGMA user_version").fetchone()[0] == 9
        assert "ping_everyone" not in {r[1] for r in source.execute("PRAGMA table_info(guilds)")}
        assert source.execute("SELECT day FROM birthdays").fetchone()[0] == 29
    assert (tmp_path / "backups").read_text() == "Existing file must not be overwritten."


async def test_invalid_backup_is_removed_and_upgrade_aborted(tmp_path):
    path = tmp_path / "birthdays.sqlite3"
    with closing(legacy_database(path)) as source:
        # A backup API failure must not leave a partial snapshot advertised as valid.
        with patch(
            "aiosqlite.Connection.backup", side_effect=sqlite3.OperationalError("disk full")
        ):
            with pytest.raises(RuntimeError, match="backup failed"):
                await Database(path).open()
        assert source.execute("PRAGMA user_version").fetchone()[0] == 9
        assert not list((tmp_path / "backups").iterdir())


async def test_failed_migration_rolls_back_and_keeps_verified_backup(tmp_path):
    path = tmp_path / "birthdays.sqlite3"
    with closing(legacy_database(path)) as source:
        with patch("birthdaybot.database.SCHEMA", SCHEMA + "\nINVALID SQL;"):
            with pytest.raises(sqlite3.OperationalError):
                await Database(path).open()
        assert source.execute("PRAGMA user_version").fetchone()[0] == 9
        assert "ping_everyone" not in {r[1] for r in source.execute("PRAGMA table_info(guilds)")}
        assert source.execute("SELECT day FROM birthdays").fetchone()[0] == 29
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


async def test_fresh_database_and_ordinary_restarts_do_not_create_backups(db):
    assert not (db.path.parent / "backups").exists()
    reopened = Database(db.path)
    await reopened.open()
    await reopened.close()
    assert not (db.path.parent / "backups").exists()


async def test_newer_schema_is_refused_without_backup_or_downgrade(db):
    async with db.transaction() as conn:
        await conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        await Database(db.path).open()
    assert (await db.read("PRAGMA user_version"))[0][0] == SCHEMA_VERSION + 1
    assert not (db.path.parent / "backups").exists()
