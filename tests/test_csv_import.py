import asyncio
import itertools
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from birthdaybot.csv_import import (
    MAX_BYTES,
    MAX_ROWS,
    ImportRow,
    ImportSharing,
    member_id,
    parse_csv,
)
from birthdaybot.sharing import FLAGS, Sharing

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


def test_granular_csv_flags_and_readable_aliases_are_recognized():
    parsed = parse_csv(
        csv_data(
            f"{A},1,25,Europe/Bucharest,false,yes,off,1,",
            header="user_id,month,day,timezone,opt_out,public_list,server_posts,birthday_dms,post_mentions",
        )
    )
    assert not parsed.issues and parsed.ignored_columns == 0
    row = parsed.rows[0]
    assert row.consent is True
    assert (row.list_public, row.announce, row.dm_enabled, row.mention_enabled) == (
        True,
        False,
        True,
        None,
    )
    assert ImportSharing().resolve(row) == Sharing(True, False, True, True)


@pytest.mark.parametrize("key", FLAGS)
def test_invalid_csv_flag_rejects_the_row_without_echoing_value(key):
    parsed = parse_csv(csv_data(f"{A},1,2,@everyone", header=f"user_id,month,day,{key}"))
    assert not parsed.rows and len(parsed.issues) == 1
    assert "@everyone" not in parsed.issues[0].message
    assert key in parsed.issues[0].message


@pytest.mark.parametrize("mode", ["off", "on", "csv"])
def test_global_optout_always_blocks_every_flag_even_explicit_optins(mode):
    row = ImportRow(2, A, 1, 1, "UTC", False, True, True, True, True)
    assert ImportSharing.all(mode).resolve(row) == Sharing.all(False)


@pytest.mark.parametrize("mode", ["off", "on", "csv"])
def test_individual_csv_optouts_are_not_overridden_by_import_defaults(mode):
    row = ImportRow(2, A, 1, 1, "UTC", True, False, False, False, False)
    assert ImportSharing.all(mode).resolve(row) == Sharing.all(False)


def test_import_modes_have_explicit_defaults_and_no_hidden_optin():
    row = ImportRow(2, A, 1, 1, "UTC", None)
    assert ImportSharing().resolve(row) == Sharing.all(False)
    assert ImportSharing.all("off").resolve(replace(row, consent=True)) == Sharing.all(False)
    assert ImportSharing.all("on").resolve(row) == Sharing.all(True)
    assert ImportSharing("on", "csv", "off", "on").resolve(row) == Sharing(True, False, False, True)


@pytest.mark.parametrize("invalid", [True, None, 1, "yes", "CSV"])
def test_invalid_import_modes_cannot_enable_sharing(invalid):
    with pytest.raises(ValueError):
        ImportSharing.all(invalid)


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
        "user_id,month,day,list_public,public_list",
        "user_id,month,day,announce,server_posts",
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


async def test_granular_import_persists_all_combinations_and_pending_choices(db):
    combinations = list(itertools.product((False, True), repeat=4))
    records = [
        f"{A + index},2,29,UTC," + ",".join(str(value).lower() for value in values)
        for index, values in enumerate(combinations)
    ]
    records.append(f"{B},3,1,,true,false,true,false")
    rows = parse_csv(
        csv_data(*records, header="user_id,month,day,timezone," + ",".join(FLAGS))
    ).rows
    assert await db.import_birthdays(1, C, rows, sharing=ImportSharing()) == (16, 1, 0)
    for index, values in enumerate(combinations):
        record = await db.birthday(1, A + index)
        assert Sharing.from_record(record) == Sharing(*values)
        assert bool(record["consent"]) == Sharing(*values).enabled
    assert Sharing.from_record(await db.pending_birthday(1, B)) == Sharing(True, False, True, False)
    assert (await db.directory(1))["visible"] == 8
    # Re-imports never overwrite a member's previous settings.
    assert await db.import_birthdays(1, C, rows, sharing=ImportSharing.all("on")) == (0, 0, 17)
    assert Sharing.from_record(await db.birthday(1, A)) == Sharing.all(False)


async def test_import_menu_defaults_apply_to_missing_flags_but_honor_explicit_refusals(db):
    rows = parse_csv(
        csv_data(
            f"{A},1,1,UTC,,,false,,",
            f"{B},1,1,UTC,true,true,true,true,true",
            f"{C},1,1,UTC,false,true,true,true,true",
            header="user_id,month,day,timezone,opt_out,list_public,announce,dm_enabled,mention_enabled",
        )
    ).rows
    await db.import_birthdays(1, A, rows, sharing=ImportSharing("on", "on", "off", "csv"))
    assert Sharing.from_record(await db.birthday(1, A)) == Sharing(True, False, False, False)
    assert Sharing.from_record(await db.birthday(1, B)) == Sharing.all(False)
    assert Sharing.from_record(await db.birthday(1, C)) == Sharing(True, True, False, True)


async def test_import_revalidates_granular_flags_before_writing_any_rows(db):
    valid = ImportRow(2, A, 1, 1, "UTC", None, True, False, True, False)
    with pytest.raises(ValueError, match="Invalid sharing"):
        await db.import_birthdays(
            1, C, [valid, replace(valid, user_id=B, announce="false")], sharing=ImportSharing()
        )
    assert not await db.read("SELECT * FROM birthdays")


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
    assert (await db.read("PRAGMA user_version"))[0][0] == 10
    record = await db.birthday(1, A)
    assert (
        record["month"],
        record["day"],
        record["timezone"],
        record["hour"],
        record["consent"],
    ) == (2, 29, "America/New_York", 18, 0)
    assert await db.pending_birthday(1, A) is None
