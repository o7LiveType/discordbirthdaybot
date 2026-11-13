import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from birthdaybot.csv_import import MAX_BYTES, MAX_ROWS, ImportRow, member_id, parse_csv

A = 111111111111111111
B = 222222222222222222
C = 333333333333333333


def csv_data(*rows, header="user_id,month,day,timezone,opt_out"):
    return (header + "\n" + "\n".join(rows) + "\n").encode()


def test_csv_handles_bom_quoted_names_aliases_month_names_and_discards_years():
    data = f'\ufeffmember_id,day,month,time_zone,consent,year,name\r\n<@!{A}>,29,February,america/new york,yes,1990,"Last, First"\r\n'.encode()
    parsed = parse_csv(data)
    assert not parsed.issues and parsed.ignored_columns == 2
    assert parsed.rows == (ImportRow(2, A, 2, 29, "America/New_York", True),)
    assert not hasattr(parsed.rows[0], "year")


@pytest.mark.parametrize(
    ("value", "consent"), [("true", False), ("no", True), ("1", False), ("off", True), ("", None)]
)
def test_optout_values_are_inverted_correctly(value, consent):
    assert parse_csv(csv_data(f"{A},1,2,UTC,{value}")).rows[0].consent is consent


def test_missing_timezone_and_sharing_are_distinct_from_utc_and_optin():
    parsed = parse_csv(csv_data(f"{A},1,2", header="id,month,day"))
    assert parsed.rows[0].timezone is None and parsed.rows[0].consent is None


@pytest.mark.parametrize(
    "record",
    [
        f"{A},4,31,UTC,false",
        f"{A},0,1,UTC,false",
        f"{A},1,0,UTC,false",
        f"{A},1,32,UTC,false",
        f"{A},Dec,10,UTC,maybe",
        f"{A},January,1,UTC+5,false",
        f"{A},January,1,@everyone,false",
        "1.7573844254470963E+17,1,2,UTC,false",
        "username,1,2,UTC,false",
        f"{A},1,2,UTC,false,extra",
        f"{A},1,2",
    ],
)
def test_invalid_rows_are_reported_without_echoing_raw_input(record):
    parsed = parse_csv(csv_data(record))
    assert not parsed.rows and len(parsed.issues) == 1
    assert parsed.issues[0].line == 2
    assert "@everyone" not in parsed.issues[0].message


@pytest.mark.parametrize(
    "header",
    [
        "user_id,id,month,day",
        "user_id,month,day,opt_out,consent",
        "name,date",
        "user_id,month,month,day",
    ],
)
def test_ambiguous_or_missing_headers_rejected(header):
    with pytest.raises(ValueError):
        parse_csv((header + "\n").encode())


def test_duplicate_ids_block_confirmation_instead_of_selecting_a_winner():
    parsed = parse_csv(csv_data(f"{A},1,1,UTC,false", f"{A},2,2,UTC,false"))
    assert len(parsed.rows) == 1 and len(parsed.issues) == 1
    assert "Duplicate" in parsed.issues[0].message


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff\xfe\x00",
        b"user_id,month,day\n\x00",
        b'user_id,month,day\n"unterminated',
        b"x" * (MAX_BYTES + 1),
    ],
)
def test_invalid_encoding_size_and_malformed_files_rejected(data):
    with pytest.raises(ValueError):
        parse_csv(data)


def test_row_limit_is_enforced_and_blank_lines_do_not_count():
    data = csv_data(*(f"{A + n},1,1,UTC,false" for n in range(MAX_ROWS)))
    assert len(parse_csv(data).rows) == MAX_ROWS
    with pytest.raises(ValueError, match="500"):
        parse_csv(data + f"{B},1,1,UTC,false\n".encode())
    assert parse_csv(csv_data("", f"{A},1,1,UTC,false", "")).total == 1


@pytest.mark.parametrize(
    "value", [f"{A}>", f"<@{A}", f"<@{A}>>", str(2**63), f"{A}.0", "-111111111111111111"]
)
def test_member_ids_reject_unbalanced_mentions_and_lossy_spreadsheet_values(value):
    with pytest.raises(ValueError):
        member_id(value)


async def test_import_is_private_by_default_atomic_idempotent_and_server_scoped(db):
    rows = parse_csv(csv_data(f"{A},2,29,UTC,false", f"{B},3,1,,true")).rows
    await db.ensure_guild(2)
    await db.save(2, A, 7, 7, "UTC", 0, 0, "feb28", True)
    await db.configure(1, None, None, "UTC", 18, 45, "", "mar1")
    assert await db.import_birthdays(1, C, rows) == (1, 1, 0)
    active = await db.birthday(1, A)
    assert active["consent"] == 0 and active["timezone"] == "UTC"
    assert (active["hour"], active["minute"], active["leap"]) == (18, 45, "mar1")
    assert await db.birthday(1, B) is None
    assert (await db.pending_birthday(1, B))["timezone"] is None
    assert (await db.directory(1))["visible"] == 0
    assert (await db.birthday(2, A))["month"] == 7
    assert await db.import_birthdays(1, C, rows, csv_sharing=True) == (0, 0, 2)
    assert (await db.birthday(1, A))["consent"] == 0


async def test_explicit_csv_sharing_requires_the_mode_and_blank_values_remain_private(db):
    rows = parse_csv(csv_data(f"{A},1,1,UTC,false", f"{B},2,2,UTC,", f"{C},3,3,UTC,true")).rows
    assert await db.import_birthdays(1, A, rows, csv_sharing=True) == (3, 0, 0)
    assert (await db.birthday(1, A))["consent"] == 1
    assert (await db.birthday(1, B))["consent"] == 0
    assert (await db.birthday(1, C))["consent"] == 0


async def test_racing_imports_never_overwrite_and_existing_rows_are_rechecked(db):
    row = ImportRow(2, A, 1, 1, "UTC", True)
    outcomes = await asyncio.gather(
        db.import_birthdays(1, C, [row]), db.import_birthdays(1, C, [replace(row, month=2)])
    )
    assert sum(saved for saved, pending, skipped in outcomes) == 1
    assert sum(skipped for saved, pending, skipped in outcomes) == 1
    assert await db.import_existing(1, [A, B]) == {A}


async def test_bulk_import_rolls_back_invalidation_and_inserts_on_failure(db, monkeypatch):
    await db.register_directory(1, 100, 500, 1000)
    row = ImportRow(2, A, 1, 1, "UTC", True)
    monkeypatch.setattr(
        db, "_invalidate_directories", AsyncMock(side_effect=RuntimeError("forced failure"))
    )
    with pytest.raises(RuntimeError):
        await db.import_birthdays(1, C, [row])
    assert await db.birthday(1, A) is None
    assert len(await db.read("SELECT * FROM directory_messages")) == 1


async def test_import_revalidates_data_and_claiming_or_deleting_removes_pending(db):
    row = ImportRow(2, A, 2, 29, None, True)
    with pytest.raises(ValueError):
        await db.import_birthdays(1, C, [replace(row, day=31)])
    await db.import_birthdays(1, C, [row], csv_sharing=True)
    await db.save(1, A, 2, 29, "Asia/Kathmandu", 0, 0, "feb28", True)
    assert await db.pending_birthday(1, A) is None
    await db.import_birthdays(1, C, [replace(row, user_id=B)])
    await db.forget(1, B)
    assert await db.pending_birthday(1, B) is None
    await db.import_birthdays(1, C, [replace(row, user_id=C)])
    await db.drop_guild(1)
    assert not await db.read("SELECT * FROM birthday_import_pending")


async def test_full_500_row_batch_does_not_exceed_old_sqlite_parameter_limit(db):
    rows = [ImportRow(n + 2, A + n, 1, 1, "UTC", None) for n in range(500)]
    assert await db.import_birthdays(1, C, rows) == (500, 0, 0)
    assert len(await db.import_existing(1, [row.user_id for row in rows])) == 500
    assert (await db.read("PRAGMA journal_mode"))[0][0] == "wal"
    assert (await db.read("PRAGMA synchronous"))[0][0] == 2


async def test_schema7_upgrade_creates_pending_table_without_changing_birthdays(db):
    await db.save(1, A, 2, 29, "America/New_York", 18, 30, "mar1", False)
    async with db.transaction() as conn:
        await conn.execute("DROP TABLE birthday_import_pending")
        await conn.execute("PRAGMA user_version=7")
    await db.close()
    await db.open()
    assert (await db.read("PRAGMA user_version"))[0][0] == 8
    record = await db.birthday(1, A)
    assert (
        record["month"],
        record["day"],
        record["timezone"],
        record["hour"],
        record["consent"],
    ) == (2, 29, "America/New_York", 18, 0)
    assert await db.pending_birthday(1, A) is None
