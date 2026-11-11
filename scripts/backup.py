"""Consistent live SQLite backup. Usage: python scripts/backup.py OUTPUT.sqlite3"""

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv


def backup(source: Path, destination: Path):
    if not source.is_file():
        raise ValueError("Source database does not exist.")
    if source.resolve() == destination.resolve():
        raise ValueError("Backup destination must differ from the live database.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation prevents accidental overwrites and symlink following.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(destination)) as snapshot:
                live.backup(snapshot, pages=256, sleep=0.1)
                if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed.")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    os.umask(0o077)
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup(Path(os.getenv("DATABASE_PATH", "data/birthdays.sqlite3")), args.destination)
    print("Backup created and integrity checked. Treat it as private data.")
