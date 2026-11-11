from datetime import UTC, date, datetime

import pytest

from birthdaybot.timeutil import anniversary, clock, due, next_occurrence, scheduled_at, timezone


@pytest.mark.parametrize("value", ["02:00", "00:00", "23:59", "09:30"])
def test_clock(value):
    assert clock(value) == tuple(map(int, value.split(":")))


@pytest.mark.parametrize("value", ["24:00", "9:00", "12:60", "-1:00", "12:00:00", "tomorrow"])
def test_invalid_clock(value):
    with pytest.raises(ValueError):
        clock(value)


@pytest.mark.parametrize("value", ["GMT+7", "../../etc/passwd", "America/Denverrr", "localtime"])
def test_invalid_zone(value):
    with pytest.raises(ValueError):
        timezone(value)


def test_leap_day():
    assert anniversary(2, 29, 2025, "feb28") == date(2025, 2, 28)
    assert anniversary(2, 29, 2025, "mar1") == date(2025, 3, 1)
    assert anniversary(2, 29, 2025, "leap_only") is None
    assert anniversary(2, 29, 2028, "leap_only") == date(2028, 2, 29)
    with pytest.raises(ValueError):
        anniversary(4, 31, 2026, "feb28")


def test_dst_gap_moves_to_first_valid_minute():
    assert scheduled_at(date(2026, 3, 8), "America/Denver", 2, 30) == datetime(
        2026, 3, 8, 9, tzinfo=UTC
    )


def test_dst_fold_uses_first_occurrence():
    assert scheduled_at(date(2026, 11, 1), "America/Denver", 1, 30) == datetime(
        2026, 11, 1, 7, 30, tzinfo=UTC
    )


def record(**changes):
    return (
        dict(month=11, day=10, timezone="America/Denver", hour=9, minute=0, leap="feb28") | changes
    )


def test_due_no_early_send_same_day_catchup_and_no_next_day():
    assert due(record(), datetime(2026, 11, 10, 15, 59, 59, tzinfo=UTC)) is None
    assert due(record(), datetime(2026, 11, 10, 16, tzinfo=UTC)) is not None
    assert due(record(), datetime(2026, 11, 11, 6, 59, tzinfo=UTC)) is not None
    assert due(record(), datetime(2026, 11, 11, 7, tzinfo=UTC)) is None


def test_offsets_year_boundary_and_quarter_hour_zones():
    value = record(month=1, day=1, timezone="Pacific/Kiritimati", hour=0)
    assert due(value, datetime(2025, 12, 31, 10, tzinfo=UTC)) == datetime(
        2025, 12, 31, 10, tzinfo=UTC
    )
    assert scheduled_at(date(2026, 11, 10), "Asia/Kathmandu", 9, 0) == datetime(
        2026, 11, 10, 3, 15, tzinfo=UTC
    )


def test_next_leap_only_and_leap_century():
    result = next_occurrence(
        record(month=2, day=29, leap="leap_only"), datetime(2096, 3, 1, tzinfo=UTC)
    )
    assert result.year == 2104


def test_skipped_date_does_not_send_on_following_day():
    value = record(month=12, day=30, timezone="Pacific/Apia")
    assert due(value, datetime(2011, 12, 30, 20, tzinfo=UTC)) is None


@pytest.mark.parametrize(
    ("zone", "month", "utc_month", "utc_day", "utc_hour", "utc_minute"),
    [
        ("America/Denver", 11, 11, 10, 7, 0),
        ("America/Denver", 7, 7, 10, 6, 0),
        ("Asia/Tokyo", 11, 11, 9, 15, 0),
        ("Asia/Kathmandu", 11, 11, 9, 18, 15),
        ("Pacific/Kiritimati", 11, 11, 9, 10, 0),
    ],
)
def test_local_midnight_is_due_at_the_configured_zone_boundary(
    zone, month, utc_month, utc_day, utc_hour, utc_minute
):
    from datetime import timedelta

    value = record(month=month, day=10, timezone=zone, hour=0, minute=0)
    target = datetime(2026, utc_month, utc_day, utc_hour, utc_minute, tzinfo=UTC)
    assert due(value, target - timedelta(seconds=1)) is None
    assert due(value, target) == target
