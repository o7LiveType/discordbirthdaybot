"""Consistent private manager cards using Discord's native embed layout."""

import calendar
from datetime import UTC, datetime

import discord

from .timeutil import due, next_occurrence, timezone_label

ACCENT = 0xDE5DBC
GIFT = (
    "https://raw.githubusercontent.com/o7LiveType/discordbirthdaybot/main/assets/birthday-gift.png"
)


def manager_embed(title, description=None):
    embed = discord.Embed(
        title=title[:256] if title else None, description=description or None, color=ACCENT
    )
    embed.set_thumbnail(url=GIFT)
    return embed


def text_embed(content):
    heading, _, body = content.partition("\n")
    title = heading.strip("* ").strip() or "Birthday manager"
    if len(title) > 256:
        body = content
        title = "Birthday manager"
    return manager_embed(title, body[:4096])


def birthday_date(month, day):
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix} {calendar.month_name[month]}"


def birthday_embed(
    user,
    record,
    *,
    settings=None,
    now=None,
    draft=False,
    zone_valid=True,
    notice=None,
    public_birthdays=None,
):
    now = now or datetime.now(UTC)
    description = notice or ("Choose your details below, then press **Save**." if draft else None)
    embed = manager_embed("Your birthday" if draft else None, description)
    name = getattr(user, "display_name", None)
    name = name if isinstance(name, str) else "Your birthday"
    avatar = getattr(getattr(user, "display_avatar", None), "url", None)
    embed.set_author(
        name=name[:256],
        **(
            {"icon_url": avatar}
            if isinstance(avatar, str) and avatar.startswith("https://")
            else {}
        ),
    )
    date = (
        birthday_date(record["month"], record["day"])
        if record["month"] and record["day"]
        else "Choose month and day below"
    )
    zone = record["timezone"]
    embed.add_field(name="Date", value=date, inline=True)
    embed.add_field(
        name="Time Zone",
        value=timezone_label(zone, now) if zone_valid else "Choose a timezone below",
        inline=True,
    )
    countdown = "Choose a valid date and timezone to see the countdown."
    if record["month"] and record["day"] and zone_valid:
        today = due(record, now)
        target = today or next_occurrence(record, now)
        stamp = int(target.timestamp())
        countdown = ("🎉 Today · " if today else "") + f"<t:{stamp}:R> (<t:{stamp}:F>)"
    embed.add_field(name="Countdown", value=countdown, inline=False)
    if record["consent"]:
        sharing = "You will be mentioned when your birthday is announced! Birthday DMs are ON."
        public = (
            public_birthdays
            if public_birthdays is not None
            else (
                settings["public_birthdays"]
                if settings is not None and "public_birthdays" in settings.keys()
                else True
            )
        )
        sharing += (
            "\nYour month and day appear in the public birthday list."
            if public
            else "\nBirthday lists are restricted to moderators in this server."
        )
    else:
        sharing = "Sharing is OFF (opted out). Your date is hidden from public lists; birthday posts and DMs are OFF."
    if settings is not None and not settings["enabled"]:
        sharing += "\nCelebrations are paused in this server."
    elif settings is not None and not settings["channel_id"]:
        sharing += "\nA moderator still needs to choose the birthday channel."
    embed.add_field(name="Birthday sharing", value=sharing, inline=False)
    embed.set_footer(
        text=f"Announcements: {record['hour']:02}:{record['minute']:02} in your timezone · Countdown dates display in your Discord timezone."
    )
    return embed
