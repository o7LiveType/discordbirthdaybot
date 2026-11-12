from datetime import UTC, datetime, timedelta

import pytest

from birthdaybot.timeutil import ZONES
from birthdaybot.timezone_catalog import REGIONS, catalog, search_timezones

JAN = datetime(2026, 1, 15, tzinfo=UTC)
JUL = datetime(2026, 7, 15, tzinfo=UTC)


def test_catalog_includes_every_installed_name_and_reuses_bounded_snapshot():
    entries = catalog(JAN)
    assert {entry.name for entry in entries} == set(ZONES)
    assert all(entry.region in REGIONS for entry in entries)
    assert catalog(JAN + timedelta(seconds=20)) is entries
    assert catalog(JAN + timedelta(minutes=1)) is not entries
    assert all(len(entry.label) <= 100 and len(entry.description) <= 100 for entry in entries)


@pytest.mark.parametrize("query", ["Europe", "America", "Africa", "Oceania", "Indian Ocean"])
def test_search_supports_regions_and_does_not_truncate(query):
    matches = search_timezones(query, catalog(JAN))
    assert matches
    if query == "America":
        assert len(matches) > 25
    if query == "Oceania":
        assert {entry.region for entry in matches} == {"Australia", "Pacific"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("New York", "America/New_York"),
        ("newyork", "America/New_York"),
        ("america/new york", "America/New_York"),
        ("Kathmandu", "Asia/Kathmandu"),
        ("St Johns", "America/St_Johns"),
    ],
)
def test_city_search_accepts_readable_names(query, expected):
    assert expected in {entry.name for entry in search_timezones(query, catalog(JAN))}


@pytest.mark.parametrize(
    ("query", "offset"),
    [
        ("UTC-05:00", -300),
        ("-0500", -300),
        ("UTC+05:45", 345),
        ("+545", 345),
        ("GMT+5:30", 330),
        ("UTC+00:00", 0),
        ("UTC+14:00", 840),
    ],
)
def test_offset_search_has_correct_sign_and_fractional_minutes(query, offset):
    matches = search_timezones(query, catalog(JAN))
    assert matches and all(entry.offset == offset for entry in matches)


def test_offset_filters_follow_dst_and_can_be_combined_with_city_or_region():
    assert "America/New_York" in {
        entry.name for entry in search_timezones("UTC-05:00", catalog(JAN))
    }
    assert "America/New_York" not in {
        entry.name for entry in search_timezones("UTC-05:00", catalog(JUL))
    }
    assert [entry.name for entry in search_timezones("New York UTC-04:00", catalog(JUL))] == [
        "America/New_York"
    ]
    assert all(
        entry.region == "Europe" and entry.offset == 60
        for entry in search_timezones("Europe UTC+01:00", catalog(JAN))
    )
    # IANA Etc/GMT sign conventions must not be misread as user-entered offsets.
    assert search_timezones("Etc/GMT+5", catalog(JAN))[0].offset == -300


@pytest.mark.parametrize(
    "query",
    [
        "UTC+25:00",
        "UTC+05:99",
        "UTC+14:30",
        "UTC\n",
        "@everyone",
        "<script>",
        "x" * 81,
        "../../etc/passwd",
        "https://breakhub.org/en/fingerprint/timezone",
    ],
)
def test_search_rejects_unsafe_or_invalid_values(query):
    with pytest.raises(ValueError):
        search_timezones(query)


def test_unknown_search_returns_no_matches_without_changing_catalog():
    entries = catalog(JAN)
    assert search_timezones("ThisCityDoesNotExist", entries) == []
    assert len(entries) == len(ZONES)
