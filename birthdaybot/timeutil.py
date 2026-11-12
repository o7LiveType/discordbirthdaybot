"""Calendar rules. Birth years are never requested or stored."""

import calendar
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

ZONES = sorted(available_timezones() - {"localtime", "posixrules"})
ZONE_KEYS = {zone.casefold(): zone for zone in ZONES}
LEAP_POLICIES = {"feb28", "mar1", "leap_only"}


def timezone_query(value: str) -> str:
    # Accept only timezone/city characters; never echo untrusted input into Markdown.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+ /-]{1,80}", value):
        raise ValueError(
            "Invalid timezone. Enter a city or IANA name using letters, spaces, /, _, + or -."
        )
    value = re.sub(" +", "_", value.strip(" "))
    if not value:
        raise ValueError("Enter a city or IANA timezone, for example New York or Europe/London.")
    return value


def timezone(value: str) -> str:
    value = ZONE_KEYS.get(timezone_query(value).casefold())
    if value is None:
        raise ValueError("Choose an IANA timezone, for example America/New_York or Europe/London.")
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("That timezone is unavailable on this host.") from exc
    return value


def find_timezones(value: str) -> list[str]:
    from .timezone_catalog import search_timezones

    matches = search_timezones(value)
    if not matches:
        raise ValueError("Timezone not found. Try another search or browse the regions.")
    return [entry.name for entry in matches]


def format_offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    hours, minutes = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02}:{minutes:02}"


def timezone_label(zone: str, now: datetime | None = None) -> str:
    zone = timezone(zone)
    offset = (now or datetime.now(UTC)).astimezone(ZoneInfo(zone)).utcoffset()
    return f"{zone} · {format_offset(int(offset.total_seconds() // 60))} now"


def clock(value: str) -> tuple[int, int]:
    try:
        if len(value) != 5 or value[2] != ":":
            raise ValueError
        hour, minute = map(int, value.split(":"))
        time(hour, minute)
        return hour, minute
    except ValueError as exc:
        raise ValueError("Use a 24-hour time in HH:MM form, for example 09:00 or 18:30.") from exc


def anniversary(month: int, day: int, year: int, leap: str) -> date | None:
    date(2000, month, day)  # Accept Feb 29, reject impossible dates.
    if leap not in LEAP_POLICIES:
        raise ValueError("Invalid leap-day policy.")
    if (month, day) == (2, 29) and not calendar.isleap(year):
        return {"feb28": date(year, 2, 28), "mar1": date(year, 3, 1), "leap_only": None}[leap]
    return date(year, month, day)


def scheduled_at(day: date, zone: str, hour: int, minute: int) -> datetime:
    """First fold on repeated times; first real minute after a DST gap.

    Round-trip via UTC detects imaginary wall times. Also handles historical
    whole-day skips; the scheduler won't send on a different local birthday.
    """
    wall = datetime.combine(day, time(hour, minute))
    tz = ZoneInfo(zone)
    for offset in range(24 * 60 + 1):
        candidate = (wall + timedelta(minutes=offset)).replace(tzinfo=tz, fold=0)
        if candidate.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == candidate.replace(
            tzinfo=None
        ):
            return candidate.astimezone(UTC)
    raise ValueError("No valid wall time within a day of the selected time.")


def due(record, now: datetime) -> datetime | None:
    local = now.astimezone(ZoneInfo(record["timezone"]))
    day = anniversary(record["month"], record["day"], local.year, record["leap"])
    if day != local.date():
        return None
    target = scheduled_at(day, record["timezone"], record["hour"], record["minute"])
    return target if now >= target else None


def next_occurrence(record, now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(record["timezone"]))
    for year in range(local.year, local.year + 9):
        day = anniversary(record["month"], record["day"], year, record["leap"])
        if day is not None:
            target = scheduled_at(day, record["timezone"], record["hour"], record["minute"])
            if target >= now and target.astimezone(ZoneInfo(record["timezone"])).date() == day:
                return target
    raise ValueError("No upcoming anniversary found.")
