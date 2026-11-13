from datetime import UTC, datetime
from types import SimpleNamespace

from birthdaybot.embeds import ACCENT, GIFT, birthday_date, birthday_embed
from birthdaybot.timeutil import next_occurrence


def user():
    return SimpleNamespace(
        display_name="Example member",
        display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/0.png"),
    )


def record(**changes):
    return (
        dict(
            month=12, day=27, timezone="Europe/London", hour=0, minute=0, leap="feb28", consent=True
        )
        | changes
    )


def test_birthday_embed_matches_reference_fields_and_uses_precise_discord_countdown():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    card = birthday_embed(user(), record(), now=now)
    fields = {field.name: field for field in card.fields}
    assert card.author.name == "Example member"
    assert card.author.icon_url == user().display_avatar.url
    assert card.color.value == ACCENT and card.thumbnail.url == GIFT
    assert fields["Date"].value == "27th December" and fields["Date"].inline
    assert (
        fields["Time Zone"].value == "Europe/London · UTC+00:00 now" and fields["Time Zone"].inline
    )
    stamp = int(next_occurrence(record(), now).timestamp())
    assert fields["Countdown"].value == f"<t:{stamp}:R> (<t:{stamp}:F>)"
    assert not fields["Countdown"].inline
    assert fields["Sharing"].value == "Public list: On\nPosts: On · Mentions: On\nDMs: On"
    assert len(card) <= 6000


def test_card_respects_optout_pause_and_pending_timezone_without_a_guessed_countdown():
    card = birthday_embed(
        user(), record(consent=False), settings={"enabled": False, "channel_id": None}
    )
    fields = {field.name: field.value for field in card.fields}
    assert fields["Sharing"] == "Public list: Off\nPosts: Off · Mentions: Off\nDMs: Off"
    paused = birthday_embed(user(), record(), settings={"enabled": False, "channel_id": None})
    assert {field.name: field.value for field in paused.fields}[
        "Sharing"
    ] == "Public list: On\nPosts: Paused · Mentions: Off\nDMs: Paused"
    pending = birthday_embed(user(), record(timezone=None), draft=True, zone_valid=False)
    fields = {field.name: field.value for field in pending.fields}
    assert fields["Time Zone"] == "Required"
    assert "Countdown" not in fields


def test_today_card_and_leap_day_card_follow_scheduler_calendar_rules():
    today = birthday_embed(user(), record(), now=datetime(2026, 12, 27, 12, tzinfo=UTC))
    assert today.fields[2].value.startswith("Today")
    leap = record(month=2, day=29, leap="mar1", timezone="America/New_York")
    now = datetime(2027, 2, 1, tzinfo=UTC)
    stamp = int(datetime(2027, 3, 1, 5, tzinfo=UTC).timestamp())
    assert f"<t:{stamp}:F>" in birthday_embed(user(), leap, now=now).fields[2].value
    assert [birthday_date(1, day) for day in (1, 2, 3, 11, 12, 13, 21, 22, 23, 31)] == [
        "1st January",
        "2nd January",
        "3rd January",
        "11th January",
        "12th January",
        "13th January",
        "21st January",
        "22nd January",
        "23rd January",
        "31st January",
    ]
