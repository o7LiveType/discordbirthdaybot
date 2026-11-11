"""One serialized connection; every mutation commits with WAL + synchronous FULL."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
 guild_id INTEGER PRIMARY KEY, channel_id INTEGER, alert_channel_id INTEGER,
 timezone TEXT NOT NULL DEFAULT 'UTC', hour INTEGER NOT NULL DEFAULT 9 CHECK(hour BETWEEN 0 AND 23),
 minute INTEGER NOT NULL DEFAULT 0 CHECK(minute BETWEEN 0 AND 59),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), gif TEXT NOT NULL DEFAULT '',
 leap TEXT NOT NULL DEFAULT 'feb28' CHECK(leap IN ('feb28','mar1','leap_only'))
);
CREATE TABLE IF NOT EXISTS birthdays (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL, month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
 day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31), timezone TEXT NOT NULL,
 hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23), minute INTEGER NOT NULL CHECK(minute BETWEEN 0 AND 59),
 leap TEXT NOT NULL CHECK(leap IN ('feb28','mar1','leap_only')),
 consent INTEGER NOT NULL CHECK(consent IN (0,1)), updated_at REAL NOT NULL,
 PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, year INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ready','sending','sent','retry','uncertain','skipped')),
 marker TEXT NOT NULL UNIQUE, channel_id INTEGER NOT NULL,
 created_at REAL NOT NULL, attempted_at REAL, retry_at REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, message_id INTEGER, error TEXT,
 PRIMARY KEY(guild_id,user_id,year),
 FOREIGN KEY(guild_id,user_id) REFERENCES birthdays(guild_id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS notices (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 code TEXT NOT NULL, last_at REAL NOT NULL, PRIMARY KEY(guild_id,code)
);
CREATE TABLE IF NOT EXISTS receipts (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL, year INTEGER NOT NULL, PRIMARY KEY(guild_id,user_id,year)
);
CREATE TABLE IF NOT EXISTS cleanup (
 message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 channel_id INTEGER NOT NULL, delete_at REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cleanup_due ON cleanup(delete_at,retry_at);
INSERT OR IGNORE INTO receipts SELECT guild_id,user_id,year FROM deliveries WHERE state='sent';
CREATE INDEX IF NOT EXISTS birthdays_active_date ON birthdays(month,day,guild_id) WHERE consent=1;
CREATE INDEX IF NOT EXISTS birthdays_guild_date ON birthdays(guild_id,month,day,user_id);
PRAGMA user_version=2;
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        try:
            await self.conn.execute("PRAGMA busy_timeout=5000")
            await self.conn.execute("PRAGMA foreign_keys=ON")
            result = await (await self.conn.execute("PRAGMA journal_mode=WAL")).fetchone()
            await self.conn.execute("PRAGMA synchronous=FULL")
            await self.conn.execute("PRAGMA wal_autocheckpoint=1000")
            mode = await (await self.conn.execute("PRAGMA synchronous")).fetchone()
            if result[0].lower() != "wal" or mode[0] != 2:
                raise RuntimeError("SQLite did not enable WAL with synchronous=FULL.")
            version = (await (await self.conn.execute("PRAGMA user_version")).fetchone())[0]
            if version not in (0, 1, 2):
                raise RuntimeError("Database schema is newer than this bot; refusing to downgrade.")
            migration = (
                "ALTER TABLE guilds ADD COLUMN leap TEXT NOT NULL DEFAULT 'feb28' CHECK(leap IN ('feb28','mar1','leap_only'));\n"
                if version == 1
                else ""
            )
            await self.conn.executescript("BEGIN IMMEDIATE;\n" + migration + SCHEMA + "\nCOMMIT;")
            self.path.chmod(0o600)
        except BaseException:
            await self.conn.close()
            raise

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def read(self, query, parameters=()):
        async with self.lock:
            async with self.conn.execute(query, parameters) as cursor:
                return await cursor.fetchall()

    async def ensure_guild(self, guild_id):
        async with self.transaction() as conn:
            await conn.execute("INSERT OR IGNORE INTO guilds(guild_id) VALUES (?)", (guild_id,))

    async def guild(self, guild_id):
        rows = await self.read("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
        if not rows:
            await self.ensure_guild(guild_id)
            rows = await self.read("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
        return rows[0]

    async def configure(
        self, guild_id, channel_id, alert_channel_id, zone, hour, minute, gif, leap="feb28"
    ):
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE guilds SET channel_id=?, alert_channel_id=?, timezone=?,
                hour=?, minute=?, gif=?, leap=? WHERE guild_id=?""",
                (channel_id, alert_channel_id, zone, hour, minute, gif, leap, guild_id),
            )
            await conn.execute(
                "UPDATE birthdays SET hour=?,minute=?,leap=? WHERE guild_id=?",
                (hour, minute, leap, guild_id),
            )

    async def pause(self, guild_id, paused):
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE guilds SET enabled=? WHERE guild_id=?", (not paused, guild_id)
            )

    async def birthday(self, guild_id, user_id):
        rows = await self.read(
            "SELECT * FROM birthdays WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        return rows[0] if rows else None

    async def save(self, guild_id, user_id, month, day, zone, hour, minute, leap, consent):
        from datetime import time

        from .timeutil import anniversary, timezone

        anniversary(month, day, 2000, leap)
        timezone(zone)
        time(hour, minute)
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO birthdays VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET month=excluded.month,day=excluded.day,
                timezone=excluded.timezone,hour=excluded.hour,minute=excluded.minute,
                leap=excluded.leap,consent=excluded.consent,updated_at=excluded.updated_at""",
                (
                    guild_id,
                    user_id,
                    month,
                    day,
                    zone,
                    hour,
                    minute,
                    leap,
                    consent,
                    datetime.now(UTC).timestamp(),
                ),
            )

    async def candidates(self, now):
        days = {(now + timedelta(hours=offset)).date() for offset in (-12, 0, 14)}
        pairs = {(day.month, day.day) for day in days}
        if any((day.month, day.day) in {(2, 28), (3, 1)} for day in days):
            pairs.add((2, 29))
        terms = " OR ".join("(b.month=? AND b.day=?)" for _ in pairs)
        parameters = [value for pair in sorted(pairs) for value in pair]
        query = f"""SELECT b.* FROM birthdays b JOIN guilds g ON g.guild_id=b.guild_id
          WHERE b.consent=1 AND g.enabled=1 AND ({terms})
          AND NOT EXISTS (SELECT 1 FROM receipts d WHERE d.guild_id=b.guild_id AND d.user_id=b.user_id
          AND d.year=CASE WHEN b.month=1 AND ?=12 THEN ?+1
                          WHEN b.month=12 AND ?=1 THEN ?-1 ELSE ? END
          ) ORDER BY b.guild_id,b.user_id"""  # noqa: S608 - fixed SQL fragments only
        return await self.read(
            query, (*parameters, now.month, now.year, now.month, now.year, now.year)
        )

    async def forget(self, guild_id, user_id):
        async with self.transaction() as conn:
            await conn.execute(
                "DELETE FROM birthdays WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )

    async def drop_guild(self, guild_id):
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM guilds WHERE guild_id=?", (guild_id,))

    async def claim(self, record, year, channel_id, now):
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT OR IGNORE INTO deliveries
                (guild_id,user_id,year,state,marker,channel_id,created_at)
                VALUES (?,?,?,'ready',?,?,?)""",
                (record["guild_id"], record["user_id"], year, uuid4().hex[:20], channel_id, now),
            )
        return (
            await self.read(
                "SELECT * FROM deliveries WHERE guild_id=? AND user_id=? AND year=?",
                (record["guild_id"], record["user_id"], year),
            )
        )[0]

    async def delivery_state(
        self,
        job,
        state,
        *,
        error=None,
        message_id=None,
        retry_at=0,
        channel_id=None,
        delete_at=None,
    ):
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE deliveries SET state=?,error=?,message_id=COALESCE(?,message_id),
                retry_at=?, attempted_at=?, attempts=attempts+?,channel_id=COALESCE(?,channel_id) WHERE marker=?""",
                (
                    state,
                    error,
                    message_id,
                    retry_at,
                    datetime.now(UTC).timestamp(),
                    int(state in {"sending", "retry"}),
                    channel_id,
                    job["marker"],
                ),
            )
            if state == "sent":
                await conn.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?,?,?)",
                    (job["guild_id"], job["user_id"], job["year"]),
                )
                if message_id is not None and delete_at is not None:
                    await conn.execute(
                        "INSERT OR IGNORE INTO cleanup(message_id,guild_id,channel_id,delete_at) VALUES (?,?,?,?)",
                        (message_id, job["guild_id"], channel_id or job["channel_id"], delete_at),
                    )

    async def cleanup_done(self, message_id):
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM cleanup WHERE message_id=?", (message_id,))

    async def cleanup_retry(self, message_id, retry_at):
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE cleanup SET attempts=attempts+1,retry_at=? WHERE message_id=?",
                (retry_at, message_id),
            )

    async def notice_due(self, guild_id, code, now, interval=3600):
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """INSERT INTO notices VALUES (?,?,?)
                ON CONFLICT(guild_id,code) DO UPDATE SET last_at=excluded.last_at
                WHERE notices.last_at<=?""",
                (guild_id, code, now, now - interval),
            )
            return cursor.rowcount == 1

    async def close(self):
        async with self.lock:
            try:
                await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                await self.conn.close()
