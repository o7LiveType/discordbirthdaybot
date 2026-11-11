from pathlib import Path

import pytest_asyncio

from birthdaybot.database import Database


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "birthdays.sqlite3")
    await database.open()
    await database.ensure_guild(1)
    yield database
    await database.close()
