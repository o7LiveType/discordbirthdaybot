"""One serialized connection; every mutation commits with WAL + synchronous FULL."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite

from .sharing import FLAGS, Sharing

log = logging.getLogger(__name__)
SCHEMA_VERSION = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
 guild_id INTEGER PRIMARY KEY, channel_id INTEGER, alert_channel_id INTEGER,
 timezone TEXT NOT NULL DEFAULT 'UTC', hour INTEGER NOT NULL DEFAULT 0 CHECK(hour BETWEEN 0 AND 23),
 minute INTEGER NOT NULL DEFAULT 0 CHECK(minute BETWEEN 0 AND 59),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), gif TEXT NOT NULL DEFAULT '',
 leap TEXT NOT NULL DEFAULT 'feb28' CHECK(leap IN ('feb28','mar1','leap_only')),
 public_birthdays INTEGER NOT NULL DEFAULT 1 CHECK(public_birthdays IN (0,1)),
 ping_everyone INTEGER NOT NULL DEFAULT 0 CHECK(ping_everyone IN (0,1))
);
CREATE TABLE IF NOT EXISTS birthdays (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL, month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
 day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31), timezone TEXT NOT NULL,
 hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23), minute INTEGER NOT NULL CHECK(minute BETWEEN 0 AND 59),
 leap TEXT NOT NULL CHECK(leap IN ('feb28','mar1','leap_only')),
 consent INTEGER NOT NULL CHECK(consent IN (0,1)), updated_at REAL NOT NULL,
 list_public INTEGER NOT NULL DEFAULT 0 CHECK(list_public IN (0,1)),
 announce INTEGER NOT NULL DEFAULT 0 CHECK(announce IN (0,1)),
 dm_enabled INTEGER NOT NULL DEFAULT 0 CHECK(dm_enabled IN (0,1)),
 mention_enabled INTEGER NOT NULL DEFAULT 0 CHECK(mention_enabled IN (0,1)),
 PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE IF NOT EXISTS birthday_import_pending (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL, month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
 day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31), timezone TEXT,
 consent INTEGER NOT NULL CHECK(consent IN (0,1)), imported_at REAL NOT NULL,
 imported_by INTEGER NOT NULL,
 list_public INTEGER NOT NULL DEFAULT 0 CHECK(list_public IN (0,1)),
 announce INTEGER NOT NULL DEFAULT 0 CHECK(announce IN (0,1)),
 dm_enabled INTEGER NOT NULL DEFAULT 0 CHECK(dm_enabled IN (0,1)),
 mention_enabled INTEGER NOT NULL DEFAULT 0 CHECK(mention_enabled IN (0,1)),
 PRIMARY KEY(guild_id,user_id)
);
CREATE INDEX IF NOT EXISTS birthday_import_pending_date ON birthday_import_pending(guild_id,month,day,user_id);
CREATE TABLE IF NOT EXISTS deliveries (
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, year INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ready','sending','sent','retry','uncertain','skipped')),
 marker TEXT NOT NULL UNIQUE, channel_id INTEGER NOT NULL,
 created_at REAL NOT NULL, attempted_at REAL, retry_at REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, message_id INTEGER, error TEXT,
 PRIMARY KEY(guild_id,user_id,year),
 FOREIGN KEY(guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS notices (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 code TEXT NOT NULL, last_at REAL NOT NULL, PRIMARY KEY(guild_id,code)
);
CREATE TABLE IF NOT EXISTS dm_deliveries (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL, year INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ready','sending','sent','retry','uncertain','blocked','skipped')),
 marker TEXT NOT NULL UNIQUE, channel_id INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 message_id INTEGER, error TEXT, PRIMARY KEY(guild_id,user_id,year)
);
CREATE INDEX IF NOT EXISTS dm_delivery_queue ON dm_deliveries(retry_at,created_at)
 WHERE state IN ('ready','retry','sending','uncertain');
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
CREATE TABLE IF NOT EXISTS directory_messages (
 guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
 channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL UNIQUE,
 PRIMARY KEY(guild_id,channel_id)
);
INSERT OR IGNORE INTO receipts SELECT guild_id,user_id,year FROM deliveries WHERE state='sent';
CREATE INDEX IF NOT EXISTS birthdays_announce_date ON birthdays(month,day,guild_id) WHERE announce=1;
CREATE INDEX IF NOT EXISTS birthdays_dm_date ON birthdays(month,day,guild_id) WHERE dm_enabled=1;
CREATE INDEX IF NOT EXISTS birthdays_guild_date ON birthdays(guild_id,month,day,user_id);
CREATE INDEX IF NOT EXISTS birthdays_visible_guild_date ON birthdays(guild_id,month,day,user_id) WHERE list_public=1;
CREATE INDEX IF NOT EXISTS deliveries_recovery ON deliveries(retry_at) WHERE state IN ('sending','uncertain');
PRAGMA user_version=10;
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
            if version not in range(SCHEMA_VERSION + 1):
                raise RuntimeError("Database schema is newer than this bot; refusing to downgrade.")
            existing = await (
                await self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
                )
            ).fetchone()
            if version < SCHEMA_VERSION and existing:
                await self._backup_before_migration(version)
            migration = (
                "ALTER TABLE guilds ADD COLUMN leap TEXT NOT NULL DEFAULT 'feb28' CHECK(leap IN ('feb28','mar1','leap_only'));\n"
                if version == 1
                else ""
            )
            if version in (1, 2):
                delivery_schema = SCHEMA[
                    SCHEMA.index("CREATE TABLE IF NOT EXISTS deliveries") : SCHEMA.index(
                        "CREATE TABLE IF NOT EXISTS notices"
                    )
                ]
                migration += delivery_schema.replace(
                    "CREATE TABLE IF NOT EXISTS deliveries", "CREATE TABLE deliveries_next"
                )
                migration += "INSERT INTO deliveries_next SELECT * FROM deliveries; DROP TABLE deliveries; ALTER TABLE deliveries_next RENAME TO deliveries;"
            if version in (1, 2, 3):
                migration += "ALTER TABLE guilds ADD COLUMN public_birthdays INTEGER NOT NULL DEFAULT 1 CHECK(public_birthdays IN (0,1));"
            if version in (1, 2, 3, 4):
                # Replace the previous 09:00 default once; preserve other configured
                # times and allow moderators to choose 09:00 again after upgrading.
                migration += """
                UPDATE birthdays SET hour=0,minute=0 WHERE guild_id IN
                  (SELECT guild_id FROM guilds WHERE hour=9 AND minute=0);
                UPDATE guilds SET hour=0 WHERE hour=9 AND minute=0;
                """
            if version in (1, 2, 3, 4, 5):
                # Only replace the old server prefill, not members' saved choices.
                migration += (
                    "UPDATE guilds SET timezone='America/New_York' WHERE timezone='America/Denver';"
                )
            if version in (1, 2, 3, 4, 5, 6):
                migration += "UPDATE guilds SET timezone='UTC' WHERE timezone='America/New_York';"
            for table in ("birthdays", "birthday_import_pending"):
                columns = {
                    row[1]
                    for row in await (
                        await self.conn.execute(f"PRAGMA table_info({table})")
                    ).fetchall()
                }  # noqa: S608 - fixed table choices
                if columns:
                    for key in FLAGS:
                        if key not in columns:
                            migration += f"ALTER TABLE {table} ADD COLUMN {key} INTEGER NOT NULL DEFAULT 0 CHECK({key} IN (0,1)); UPDATE {table} SET {key}=consent;"  # noqa: S608 - fixed table and flag names
            if version < 9:
                migration += "DROP INDEX IF EXISTS birthdays_active_date; DROP INDEX IF EXISTS birthdays_public_guild_date;"
            guild_columns = {
                row[1]
                for row in await (await self.conn.execute("PRAGMA table_info(guilds)")).fetchall()
            }
            if guild_columns and "ping_everyone" not in guild_columns:
                migration += "ALTER TABLE guilds ADD COLUMN ping_everyone INTEGER NOT NULL DEFAULT 0 CHECK(ping_everyone IN (0,1));"
            await self.conn.executescript("BEGIN IMMEDIATE;\n" + migration + SCHEMA + "\nCOMMIT;")
            self.path.chmod(0o600)
            if version < SCHEMA_VERSION:
                log.info("database_migrated from_version=%s to_version=%s", version, SCHEMA_VERSION)
        except BaseException:
            await self.conn.close()
            raise

    async def _backup_before_migration(self, version):
        """Capture committed WAL contents before changing an existing schema."""
        directory = self.path.parent / "backups"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = directory / (
            f"{self.path.stem}-before-v{version}-to-v{SCHEMA_VERSION}-{stamp}-{uuid4().hex[:8]}.sqlite3"
        )
        created = False
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            os.close(fd)
            async with aiosqlite.connect(destination, isolation_level=None) as snapshot:
                await snapshot.execute("PRAGMA synchronous=FULL")
                await self.conn.backup(snapshot, pages=256, sleep=0.1)
                result = await (await snapshot.execute("PRAGMA integrity_check")).fetchall()
                if [row[0] for row in result] != ["ok"]:
                    raise RuntimeError("Backup integrity check failed.")
        except BaseException as exc:
            if created:
                destination.unlink(missing_ok=True)
            if not isinstance(exc, Exception):
                raise
            raise RuntimeError(
                "Database backup failed; schema upgrade was not started. Check storage and permissions."
            ) from exc
        log.info(
            "database_backup_created path=%s from_version=%s to_version=%s",
            destination,
            version,
            SCHEMA_VERSION,
        )

    @asynccontextmanager
    async def transaction(self, *, write=True):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
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
            # Older database files still have the legacy SQL column default.
            await conn.execute(
                "INSERT OR IGNORE INTO guilds(guild_id,timezone,hour,minute) VALUES (?,'UTC',0,0)",
                (guild_id,),
            )

    async def guild(self, guild_id):
        rows = await self.read("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
        if not rows:
            await self.ensure_guild(guild_id)
            rows = await self.read("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
        return rows[0]

    async def configure(
        self,
        guild_id,
        channel_id,
        alert_channel_id,
        zone,
        hour,
        minute,
        gif,
        leap="feb28",
        public_birthdays=None,
        ping_everyone=None,
    ):
        async with self.transaction() as conn:
            previous = await (
                await conn.execute(
                    "SELECT public_birthdays,hour,minute,leap FROM guilds WHERE guild_id=?",
                    (guild_id,),
                )
            ).fetchone()
            await conn.execute(
                """UPDATE guilds SET channel_id=?, alert_channel_id=?, timezone=?,
                hour=?, minute=?, gif=?, leap=?, public_birthdays=COALESCE(?,public_birthdays),
                ping_everyone=COALESCE(?,ping_everyone) WHERE guild_id=?""",
                (
                    channel_id,
                    alert_channel_id,
                    zone,
                    hour,
                    minute,
                    gif,
                    leap,
                    public_birthdays,
                    ping_everyone,
                    guild_id,
                ),
            )
            await conn.execute(
                "UPDATE birthdays SET hour=?,minute=?,leap=? WHERE guild_id=?",
                (hour, minute, leap, guild_id),
            )
            if public_birthdays is not None and previous and previous[0] != public_birthdays:
                await self._invalidate_directories(conn, guild_id)
            if previous and tuple(previous[1:]) != (hour, minute, leap):
                await conn.execute(
                    "UPDATE dm_deliveries SET retry_at=0 WHERE guild_id=? AND state IN ('ready','retry')",
                    (guild_id,),
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

    async def pending_birthday(self, guild_id, user_id):
        rows = await self.read(
            "SELECT * FROM birthday_import_pending WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        return rows[0] if rows else None

    @staticmethod
    def import_query(guild_id, user_ids):
        values = ",".join("(?)" for _ in user_ids)
        query = f"WITH incoming(user_id) AS (VALUES {values}) SELECT user_id FROM birthdays WHERE guild_id=? AND user_id IN (SELECT user_id FROM incoming) UNION SELECT user_id FROM birthday_import_pending WHERE guild_id=? AND user_id IN (SELECT user_id FROM incoming)"  # noqa: S608 - generated placeholders only
        return query, (*user_ids, guild_id, guild_id)

    async def import_existing(self, guild_id, user_ids):
        if not user_ids:
            return set()
        query, params = self.import_query(guild_id, user_ids)
        return {row[0] for row in await self.read(query, params)}

    async def import_birthdays(self, guild_id, actor, rows, *, csv_sharing=False):
        from .csv_import import MAX_ROWS, validate_row

        if not rows or len(rows) > MAX_ROWS or len({row.user_id for row in rows}) != len(rows):
            raise ValueError("Invalid import batch size or duplicate members.")
        for row in rows:
            validate_row(row)
        async with self.transaction() as conn:
            settings = await (
                await conn.execute("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
            ).fetchone()
            if not settings:
                raise ValueError("This server is no longer configured.")
            ids = [row.user_id for row in rows]
            query, params = self.import_query(guild_id, ids)
            existing = {row[0] for row in await (await conn.execute(query, params)).fetchall()}
            complete, pending = [], []
            now = datetime.now(UTC).timestamp()
            for row in rows:
                if row.user_id in existing:
                    continue
                consent = bool(csv_sharing and row.consent is True)
                if row.timezone:
                    complete.append(
                        (
                            guild_id,
                            row.user_id,
                            row.month,
                            row.day,
                            row.timezone,
                            settings["hour"],
                            settings["minute"],
                            settings["leap"],
                            consent,
                            now,
                            *Sharing.all(consent).values(),
                        )
                    )
                else:
                    pending.append(
                        (
                            guild_id,
                            row.user_id,
                            row.month,
                            row.day,
                            None,
                            consent,
                            now,
                            actor,
                            *Sharing.all(consent).values(),
                        )
                    )
            await conn.executemany(
                "INSERT INTO birthdays(guild_id,user_id,month,day,timezone,hour,minute,leap,consent,updated_at,list_public,announce,dm_enabled,mention_enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                complete,
            )
            await conn.executemany(
                "INSERT INTO birthday_import_pending(guild_id,user_id,month,day,timezone,consent,imported_at,imported_by,list_public,announce,dm_enabled,mention_enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                pending,
            )
            if complete:
                await self._invalidate_directories(conn, guild_id)
        return len(complete), len(pending), len(existing)

    async def save(
        self, guild_id, user_id, month, day, zone, hour, minute, leap, consent, *, sharing=None
    ):
        from datetime import time

        from .timeutil import anniversary, timezone

        sharing = Sharing.all(consent) if sharing is None else sharing
        if not isinstance(sharing, Sharing):
            raise ValueError("Invalid sharing permissions.")
        anniversary(month, day, 2000, leap)
        zone = timezone(zone)
        time(hour, minute)
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO birthdays(guild_id,user_id,month,day,timezone,hour,minute,leap,consent,updated_at,list_public,announce,dm_enabled,mention_enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET month=excluded.month,day=excluded.day,
                timezone=excluded.timezone,hour=excluded.hour,minute=excluded.minute,
                leap=excluded.leap,consent=excluded.consent,updated_at=excluded.updated_at,
                list_public=excluded.list_public,announce=excluded.announce,dm_enabled=excluded.dm_enabled,mention_enabled=excluded.mention_enabled""",
                (
                    guild_id,
                    user_id,
                    month,
                    day,
                    zone,
                    hour,
                    minute,
                    leap,
                    sharing.enabled,
                    datetime.now(UTC).timestamp(),
                    *sharing.values(),
                ),
            )
            await self._invalidate_directories(conn, guild_id)
            await conn.execute(
                "DELETE FROM birthday_import_pending WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
            # Pending greetings follow the latest date, timezone and opt-out.
            # Sent, blocked and ambiguous jobs remain as annual anti-spam receipts.
            await conn.execute(
                "DELETE FROM dm_deliveries WHERE guild_id=? AND user_id=? AND state IN ('ready','retry','skipped')",
                (guild_id, user_id),
            )

    async def set_sharing(self, guild_id, user_id, sharing):
        if not isinstance(sharing, Sharing):
            raise ValueError("Invalid sharing permissions.")
        async with self.transaction() as conn:
            record = await (
                await conn.execute(
                    "SELECT list_public,dm_enabled FROM birthdays WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
            ).fetchone()
            pending = (
                await (
                    await conn.execute(
                        "SELECT user_id,dm_enabled FROM birthday_import_pending WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                ).fetchone()
                if record is None
                else None
            )
            if record is None and pending is None:
                raise ValueError("Birthday deleted. Use Set birthday.")
            table = "birthdays" if record is not None else "birthday_import_pending"
            timestamp = ",updated_at=?" if record is not None else ""
            await conn.execute(
                f"UPDATE {table} SET consent=?,list_public=?,announce=?,dm_enabled=?,mention_enabled=?{timestamp} WHERE guild_id=? AND user_id=?",  # noqa: S608 - fixed table and column choices
                (
                    sharing.enabled,
                    *sharing.values(),
                    *((datetime.now(UTC).timestamp(),) if record is not None else ()),
                    guild_id,
                    user_id,
                ),
            )
            if record is not None and bool(record[0]) != sharing.list_public:
                await self._invalidate_directories(conn, guild_id)
            if bool((record if record is not None else pending)[1]) != sharing.dm_enabled:
                await conn.execute(
                    "DELETE FROM dm_deliveries WHERE guild_id=? AND user_id=? AND state IN ('ready','retry','skipped')",
                    (guild_id, user_id),
                )

    async def directory(self, guild_id, *, public_only=True, month=None, page=0):
        """Return one indexed calendar page and aggregate counts in one snapshot."""
        if month is not None and month not in range(1, 13):
            raise ValueError("Choose a month from January through December.")
        public_clause = " AND list_public=1" if public_only else ""
        month_clause = " AND month=?" if month is not None else ""
        async with self.transaction(write=False) as conn:
            configured = (
                await (
                    await conn.execute(
                        "SELECT count(*) FROM birthdays WHERE guild_id=?", (guild_id,)
                    )
                ).fetchone()
            )[0]
            counts_query = (
                "SELECT month,count(*) FROM birthdays WHERE guild_id=?"  # noqa: S608 - fixed SQL fragments
                + public_clause
                + " GROUP BY month ORDER BY month"
            )
            counts = await (await conn.execute(counts_query, (guild_id,))).fetchall()
            monthly = {row[0]: row[1] for row in counts}
            visible = sum(monthly.values())
            filtered = monthly.get(month, 0) if month is not None else visible
            pages = max(1, (filtered + 9) // 10)
            page = max(0, min(page, pages - 1))
            parameters = (
                (guild_id, month, page * 10) if month is not None else (guild_id, page * 10)
            )
            rows_query = (
                "SELECT user_id,month,day FROM birthdays WHERE guild_id=?"  # noqa: S608 - fixed SQL fragments
                + public_clause
                + month_clause
                + " ORDER BY month,day,user_id LIMIT 10 OFFSET ?"
            )
            rows = await (await conn.execute(rows_query, parameters)).fetchall()
        return {
            "configured": configured,
            "visible": visible,
            "months": monthly,
            "rows": rows,
            "page": page,
            "pages": pages,
            "filtered": filtered,
        }

    async def _invalidate_directories(self, conn, guild_id):
        # Queue removal of old public snapshots whenever dates or visibility change.
        await conn.execute(
            "UPDATE cleanup SET delete_at=?,retry_at=0 WHERE message_id IN (SELECT message_id FROM directory_messages WHERE guild_id=?)",
            (datetime.now(UTC).timestamp(), guild_id),
        )
        await conn.execute("DELETE FROM directory_messages WHERE guild_id=?", (guild_id,))

    async def directory_message(self, guild_id, channel_id):
        rows = await self.read(
            "SELECT d.* FROM directory_messages d JOIN cleanup c ON c.message_id=d.message_id WHERE d.guild_id=? AND d.channel_id=? AND c.delete_at>?",
            (guild_id, channel_id, datetime.now(UTC).timestamp()),
        )
        return rows[0] if rows else None

    async def register_directory(self, guild_id, channel_id, message_id, created_at):
        async with self.transaction() as conn:
            await conn.execute(
                "INSERT INTO directory_messages VALUES (?,?,?) ON CONFLICT(guild_id,channel_id) DO UPDATE SET message_id=excluded.message_id",
                (guild_id, channel_id, message_id),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO cleanup(message_id,guild_id,channel_id,delete_at) VALUES (?,?,?,?)",
                (message_id, guild_id, channel_id, created_at + 86400),
            )

    async def candidates(self, now, *, dm=False):
        days = {(now + timedelta(hours=offset)).date() for offset in (-12, 0, 14)}
        pairs = {(day.month, day.day) for day in days}
        if any((day.month, day.day) in {(2, 28), (3, 1)} for day in days):
            pairs.add((2, 29))
        terms = " OR ".join("(b.month=? AND b.day=?)" for _ in pairs)
        parameters = [value for pair in sorted(pairs) for value in pair]
        receipt_table = "dm_deliveries" if dm else "receipts"
        permission = "dm_enabled" if dm else "announce"
        query = f"""SELECT b.* FROM birthdays b JOIN guilds g ON g.guild_id=b.guild_id
          WHERE b.{permission}=1 AND g.enabled=1 AND ({terms})
          AND NOT EXISTS (SELECT 1 FROM {receipt_table} d WHERE d.guild_id=b.guild_id AND d.user_id=b.user_id
          AND d.year=CASE WHEN b.month=1 AND ?=12 THEN ?+1
                          WHEN b.month=12 AND ?=1 THEN ?-1 ELSE ? END
          ) ORDER BY b.guild_id,b.user_id"""  # noqa: S608 - fixed SQL fragments only
        return await self.read(
            query, (*parameters, now.month, now.year, now.month, now.year, now.year)
        )

    async def forget(self, guild_id, user_id):
        async with self.transaction() as conn:
            await conn.execute(
                "DELETE FROM birthday_import_pending WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
            # Preserve unresolved send markers until their public post can be found
            # and scheduled for deletion; the private birthday itself is erased.
            await conn.execute(
                "DELETE FROM deliveries WHERE guild_id=? AND user_id=? AND state NOT IN ('sending','uncertain')",
                (guild_id, user_id),
            )
            await conn.execute(
                "DELETE FROM birthdays WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            await self._invalidate_directories(conn, guild_id)
            await conn.execute(
                "DELETE FROM dm_deliveries WHERE guild_id=? AND user_id=? AND state IN ('ready','retry','skipped')",
                (guild_id, user_id),
            )

    async def claim_dm(self, record, year, now):
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT OR IGNORE INTO dm_deliveries
                (guild_id,user_id,year,state,marker,created_at) VALUES (?,?,?,'ready',?,?)""",
                (record["guild_id"], record["user_id"], year, uuid4().hex[:20], now),
            )
        return await self.dm_job(record["guild_id"], record["user_id"], year)

    async def dm_job(self, guild_id, user_id, year):
        rows = await self.read(
            "SELECT * FROM dm_deliveries WHERE guild_id=? AND user_id=? AND year=?",
            (guild_id, user_id, year),
        )
        return rows[0] if rows else None

    async def dm_state(
        self,
        job,
        state,
        *,
        error=None,
        retry_at=0,
        channel_id=None,
        message_id=None,
        delete_at=None,
    ):
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE dm_deliveries SET state=?,error=?,retry_at=?,
                channel_id=COALESCE(?,channel_id),message_id=COALESCE(?,message_id),attempts=attempts+?
                WHERE marker=?""",
                (
                    state,
                    error,
                    retry_at,
                    channel_id,
                    message_id,
                    int(state in {"sending", "retry"}),
                    job["marker"],
                ),
            )
            if state == "sent" and message_id is not None and delete_at is not None:
                await conn.execute(
                    "INSERT OR IGNORE INTO cleanup(message_id,guild_id,channel_id,delete_at) VALUES (?,?,?,?)",
                    (message_id, job["guild_id"], channel_id or job["channel_id"], delete_at),
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
            await conn.execute("DELETE FROM directory_messages WHERE message_id=?", (message_id,))
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
