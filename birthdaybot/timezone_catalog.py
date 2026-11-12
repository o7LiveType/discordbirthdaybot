"""Local timezone browsing; no network, browser fingerprinting or location lookup."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from .timeutil import ZONE_KEYS, ZONES, format_offset, timezone

REGIONS = {
    "Africa": "Africa",
    "America": "Americas",
    "Antarctica": "Antarctica",
    "Arctic": "Arctic",
    "Asia": "Asia",
    "Atlantic": "Atlantic Ocean",
    "Australia": "Australia",
    "Europe": "Europe",
    "Indian": "Indian Ocean",
    "Pacific": "Pacific Ocean",
    "Other": "Other names & fixed offsets",
}


@dataclass(frozen=True)
class ZoneEntry:
    name: str
    region: str
    offset: int

    @property
    def label(self):
        return self.name.replace("_", " ").replace("/", " / ")

    @property
    def description(self):
        return f"{REGIONS[self.region]} · {format_offset(self.offset)} now"


@lru_cache(maxsize=2)
def _catalog(minute):
    now = datetime.fromtimestamp(minute * 60, UTC)
    return tuple(
        ZoneEntry(
            name,
            name.split("/")[0] if name.split("/")[0] in REGIONS else "Other",
            int(now.astimezone(ZoneInfo(name)).utcoffset().total_seconds() // 60),
        )
        for name in ZONES
    )


def catalog(now=None):
    # One shared, bounded snapshot per minute, independent of server/user count.
    return _catalog(int((now or datetime.now(UTC)).timestamp() // 60))


def search_timezones(query, entries=None):
    if not isinstance(query, str) or not re.fullmatch(r"[A-Za-z0-9_+ /:\-]{1,80}", query):
        raise ValueError("Use a city, region or UTC offset, such as New York, Europe or UTC+05:45.")
    query = query.strip(" ")
    if not query:
        raise ValueError("Enter a city, region or UTC offset, or use the region menus.")
    entries = catalog() if entries is None else entries
    exact = ZONE_KEYS.get(re.sub(" +", "_", query).casefold())
    if exact:
        return [entry for entry in entries if entry.name == timezone(exact)]
    offset = None
    match = re.search(
        r"(?<![A-Za-z0-9_/])(?:(?:UTC|GMT)\s*)?[+-]\d{1,2}(?::?\d{2})?(?![\d:])",
        query,
        re.IGNORECASE,
    )
    if match:
        parts = re.fullmatch(
            r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", match[0], re.IGNORECASE
        )
        hours, minutes = int(parts[2]), int(parts[3] or 0)
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ValueError("Invalid UTC offset. Use a value such as UTC-05:00 or UTC+05:45.")
        offset = (hours * 60 + minutes) * (-1 if parts[1] == "-" else 1)
        query = query[: match.start()] + query[match.end() :]
    words = re.findall(r"[a-z0-9]+", query.casefold().replace("_", " "))
    if not words and offset is None:
        raise ValueError("Enter a city, region or UTC offset, or use the region menus.")
    matches = []
    for entry in entries:
        terms = f"{entry.name} {REGIONS[entry.region]}"
        if entry.region in {"Australia", "Pacific"}:
            terms += " Oceania"
        compact = re.sub(r"[^a-z0-9]", "", terms.casefold())
        if (offset is None or entry.offset == offset) and all(word in compact for word in words):
            matches.append(entry)
    return matches
