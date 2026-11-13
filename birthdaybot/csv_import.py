"""Bounded CSV parsing. File bytes, ignored birth years and raw errors are never logged."""

import calendar
import csv
import io
import re
from dataclasses import dataclass
from typing import NamedTuple

from .sharing import FLAGS, Sharing
from .timeutil import anniversary, timezone

MAX_BYTES = 256 * 1024
MAX_ROWS = 500
HEADERS = {
    "user_id": "user_id",
    "member_id": "user_id",
    "discord_id": "user_id",
    "id": "user_id",
    "month": "month",
    "birth_month": "month",
    "day": "day",
    "birth_day": "day",
    "timezone": "timezone",
    "time_zone": "timezone",
    "tz": "timezone",
    "opt_out": "opt_out",
    "consent": "consent",
    "sharing": "consent",
    "show_birthday": "consent",
    **{key: key for key in FLAGS},
    "public_list": "list_public",
    "server_posts": "announce",
    "birthday_dms": "dm_enabled",
    "post_mentions": "mention_enabled",
}
MONTHS = {
    name.casefold(): number
    for number in range(1, 13)
    for name in (calendar.month_name[number], calendar.month_abbr[number])
}
MONTHS["sept"] = 9


@dataclass(frozen=True)
class ImportRow:
    line: int
    user_id: int
    month: int
    day: int
    timezone: str | None
    consent: bool | None
    list_public: bool | None = None
    announce: bool | None = None
    dm_enabled: bool | None = None
    mention_enabled: bool | None = None


@dataclass(frozen=True)
class ImportSharing:
    """Per-flag import defaults; explicit CSV refusals always take precedence."""

    list_public: str = "csv"
    announce: str = "csv"
    dm_enabled: str = "csv"
    mention_enabled: str = "csv"

    def __post_init__(self):
        if any(
            type(getattr(self, key)) is not str or getattr(self, key) not in {"off", "on", "csv"}
            for key in FLAGS
        ):
            raise ValueError("Import sharing must be Off, On or From CSV.")

    @classmethod
    def all(cls, mode):
        return cls(*(mode for _ in FLAGS))

    def resolve(self, row):
        if row.consent is False:
            return Sharing.all(False)
        values = []
        for key in FLAGS:
            mode, explicit = getattr(self, key), getattr(row, key)
            values.append(
                mode != "off"
                and explicit is not False
                and (mode == "on" or explicit is True or row.consent is True)
            )
        return Sharing(*values)


@dataclass(frozen=True)
class ImportIssue:
    line: int
    message: str


@dataclass(frozen=True)
class ParsedImport:
    rows: tuple[ImportRow, ...]
    issues: tuple[ImportIssue, ...]
    ignored_columns: int
    total: int


@dataclass(frozen=True)
class ImportPlan:
    added: int
    updated: int
    removed: int


class ImportResult(NamedTuple):
    saved: int
    pending: int
    added: int
    updated: int
    removed: int


def member_id(value):
    match = re.fullmatch(r"(?:<@!?)?([0-9]{1,19})>?", value)
    if not match or value.startswith("<") != value.endswith(">"):
        raise ValueError(
            "Member ID must be an exact Discord user ID or mention; names and spreadsheet scientific notation are not accepted."
        )
    number = int(match[1])
    if not 1 <= number <= 2**63 - 1:
        raise ValueError("Member ID is outside the supported Discord ID range.")
    return number


def boolean(value):
    if not value:
        return None
    if value.casefold() in {"true", "yes", "1", "on"}:
        return True
    if value.casefold() in {"false", "no", "0", "off"}:
        return False
    raise ValueError("Sharing flags must be true/false, yes/no, on/off or 1/0.")


def validate_row(row):
    if member_id(str(row.user_id)) != row.user_id:
        raise ValueError("Invalid member ID.")
    anniversary(row.month, row.day, 2000, "feb28")
    if row.timezone is not None and timezone(row.timezone) != row.timezone:
        raise ValueError("Timezone must use its canonical IANA name.")
    if any(
        value is not None and type(value) is not bool
        for value in (row.consent, *(getattr(row, key) for key in FLAGS))
    ):
        raise ValueError("Invalid sharing choice.")


def parse_csv(data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Upload a non-empty CSV no larger than 256 KiB.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("Save the CSV as UTF-8, then upload it again.") from exc
    if "\x00" in content:
        raise ValueError("The CSV contains invalid control characters.")
    reader = csv.reader(io.StringIO(content, newline=""), strict=True)
    try:
        header = next(reader)
        if len(header) > 30:
            raise ValueError("The CSV has too many columns (maximum 30).")
        keys = [HEADERS.get(value.strip().casefold().replace(" ", "_")) for value in header]
        named = [key for key in keys if key]
        if len(named) != len(set(named)) or ("opt_out" in named and "consent" in named):
            raise ValueError(
                "Duplicate or conflicting CSV headers. Supply each field once; use opt_out or consent, not both."
            )
        if not {"user_id", "month", "day"} <= set(named):
            raise ValueError(
                "CSV headers must include user_id (or member_id), month and day. Optional: timezone, opt_out, list_public, announce, dm_enabled, mention_enabled."
            )
        rows, issues, seen, total = [], [], {}, 0
        for fields in reader:
            if not fields or not any(value.strip() for value in fields):
                continue
            total += 1
            if total > MAX_ROWS:
                raise ValueError(
                    "Replacement CSV supports at most 500 records. Include the complete server list in one file."
                )
            line = reader.line_num
            try:
                if len(fields) != len(header) or any(len(value) > 2048 for value in fields):
                    raise ValueError("Column count differs from the header or a field is too long.")
                values = {
                    key: value.strip(" ") for key, value in zip(keys, fields, strict=True) if key
                }
                user = member_id(values["user_id"])
                if user in seen:
                    raise ValueError(f"Duplicate member ID; first appears on line {seen[user]}.")
                seen[user] = line
                month_text = values["month"].casefold()
                month = MONTHS.get(month_text)
                if month is None:
                    if not re.fullmatch(r"[0-9]{1,2}", month_text):
                        raise ValueError("Month must be 1–12 or an English month name.")
                    month = int(month_text)
                if not re.fullmatch(r"[0-9]{1,2}", values["day"]):
                    raise ValueError("Day must be a number from 1–31.")
                day = int(values["day"])
                try:
                    anniversary(month, day, 2000, "feb28")
                except ValueError as exc:
                    raise ValueError(
                        "Invalid month/day combination. February 29 is allowed."
                    ) from exc
                zone = timezone(values["timezone"]) if values.get("timezone") else None
                sharing = boolean(values.get("opt_out", values.get("consent", "")))
                if "opt_out" in values and sharing is not None:
                    sharing = not sharing
                permissions = {}
                for key in FLAGS:
                    try:
                        permissions[key] = boolean(values.get(key, ""))
                    except ValueError as exc:
                        raise ValueError(f"{key}: {exc}") from exc
                row = ImportRow(
                    line,
                    user,
                    month,
                    day,
                    zone,
                    sharing,
                    **permissions,
                )
                validate_row(row)
                rows.append(row)
            except ValueError as exc:
                issues.append(ImportIssue(line, str(exc)))
    except (csv.Error, StopIteration) as exc:
        raise ValueError(
            "Malformed CSV. Use a comma-separated UTF-8 file with a header and correctly quoted fields."
        ) from exc
    return ParsedImport(tuple(rows), tuple(issues), len(header) - len(named), total)
