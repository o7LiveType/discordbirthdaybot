import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from birthdaybot.process_lock import process_lock
from scripts.backup import backup


async def test_live_backup_includes_uncheckpointed_wal_and_is_private(db, tmp_path):
    await db.save(1, 2, 11, 10, "America/New_York", 9, 0, "feb28", True)
    destination = tmp_path / "backup.sqlite3"
    backup(db.path, destination)
    with closing(sqlite3.connect(destination)) as snapshot:
        assert snapshot.execute("SELECT day FROM birthdays").fetchone()[0] == 10
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        backup(db.path, destination)
    with pytest.raises(ValueError):
        backup(db.path, db.path)


def test_only_one_process_can_own_database(tmp_path):
    path = tmp_path / "bot.lock"
    with process_lock(path):
        with pytest.raises(RuntimeError):
            with process_lock(path):
                pass
    with process_lock(path):
        pass


def test_committed_record_survives_process_exit_without_checkpoint(tmp_path):
    path = tmp_path / "crash.sqlite3"
    script = """
import asyncio, os, sys
from pathlib import Path
from birthdaybot.database import Database
async def run():
    db = Database(Path(sys.argv[1]))
    await db.open()
    await db.ensure_guild(1)
    await db.save(1,2,11,10,'America/New_York',9,0,'feb28',True)
    job = await db.claim(await db.birthday(1,2),2026,100,1000)
    await db.delivery_state(job,'sending')
    os._exit(0)
asyncio.run(run())
"""
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, timeout=20)
    with closing(sqlite3.connect(path)) as recovered:
        assert recovered.execute("SELECT state FROM deliveries").fetchone()[0] == "sending"
        assert recovered.execute("SELECT day FROM birthdays").fetchone()[0] == 10
        assert recovered.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
