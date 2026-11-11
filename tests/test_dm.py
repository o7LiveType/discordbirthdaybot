import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from test_delivery import NOW, empty_history, fixture

from birthdaybot.delivery import Scheduler
from birthdaybot.dm import greeting


async def test_dm_names_server_and_is_once_per_year_across_restarts_and_re_registration(db):
    scheduler, guild, channel = await fixture(db)
    dm = guild.fetch_member.return_value.create_dm.return_value
    await scheduler.tick(NOW)
    await Scheduler(scheduler.bot).tick(NOW + timedelta(minutes=1))
    await db.forget(1, 2)
    await db.save(1, 2, 11, 10, "America/New_York", 9, 0, "feb28", True)
    await Scheduler(scheduler.bot).tick(NOW + timedelta(minutes=2))
    dm.send.assert_awaited_once()
    channel.send.assert_awaited_once()
    args = dm.send.call_args.kwargs
    assert "Test server" in args["embed"].description
    assert args["nonce"]
    assert not args["allowed_mentions"].users and not args["allowed_mentions"].everyone
    assert (await db.dm_job(1, 2, 2026))["state"] == "sent"
    assert len(await db.read("SELECT * FROM cleanup")) == 2


async def test_blocked_dm_does_not_interrupt_channel_post_or_retry_this_year(db):
    scheduler, guild, channel = await fixture(db)
    dm = guild.fetch_member.return_value.create_dm.return_value
    dm.send.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user"
    )
    await scheduler.tick(NOW)
    await Scheduler(scheduler.bot).tick(NOW + timedelta(hours=1))
    await db.forget(1, 2)
    await db.save(1, 2, 11, 10, "America/New_York", 9, 0, "feb28", True)
    await scheduler.tick(NOW + timedelta(hours=2))
    dm.send.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert (await db.dm_job(1, 2, 2026))["state"] == "blocked"


@pytest.mark.parametrize("failure", ["forbidden", "missing"])
async def test_dm_is_independent_of_public_channel_failure(db, failure):
    scheduler, guild, channel = await fixture(db)
    if failure == "forbidden":
        channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Denied"
        )
    else:
        guild.get_channel.return_value = None
    await scheduler.tick(NOW)
    guild.fetch_member.return_value.create_dm.return_value.send.assert_awaited_once()
    assert (await db.dm_job(1, 2, 2026))["state"] == "sent"
    assert (await db.read("SELECT state FROM deliveries"))[0][0] == "retry"


@pytest.mark.parametrize("case", ["optout", "paused", "future", "past", "departed"])
async def test_no_unwanted_or_wrong_day_dms(db, case):
    scheduler, guild, channel = await fixture(db)
    dm = guild.fetch_member.return_value.create_dm.return_value
    if case == "optout":
        await db.save(1, 2, 11, 10, "America/New_York", 9, 0, "feb28", False)
    elif case == "paused":
        await db.pause(1, True)
    elif case == "departed":
        guild.fetch_member.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown member"
        )
    now = (
        NOW - timedelta(hours=2)
        if case == "future"
        else NOW + timedelta(days=1)
        if case == "past"
        else NOW
    )
    await scheduler.tick(now)
    dm.send.assert_not_awaited()


async def test_transient_failure_retries_after_restart_without_repeating_public_post(db):
    scheduler, guild, channel = await fixture(db)
    member = guild.fetch_member.return_value
    dm = member.create_dm.return_value
    member.create_dm.side_effect = [OSError("connection lost"), dm]
    await scheduler.tick(NOW)
    assert (await db.dm_job(1, 2, 2026))["state"] == "retry"
    await Scheduler(scheduler.bot).tick(NOW + timedelta(seconds=30))
    assert member.create_dm.await_count == 1
    await Scheduler(scheduler.bot).tick(NOW + timedelta(minutes=2))
    dm.send.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert (await db.dm_job(1, 2, 2026))["state"] == "sent"


async def test_ambiguous_dm_is_not_blindly_resent(db):
    scheduler, guild, channel = await fixture(db)
    dm = guild.fetch_member.return_value.create_dm.return_value
    dm.send.side_effect = TimeoutError()
    scheduler.bot.get_partial_messageable.return_value.history = empty_history
    await scheduler.tick(NOW)
    await Scheduler(scheduler.bot).tick(NOW + timedelta(minutes=2))
    dm.send.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert (await db.dm_job(1, 2, 2026))["state"] == "uncertain"


async def test_interrupted_dm_is_found_and_expired_after_date_and_record_deletion(db):
    scheduler, guild, channel = await fixture(db)
    dm = guild.fetch_member.return_value.create_dm.return_value
    job = await db.claim_dm(await db.birthday(1, 2), 2026, NOW.timestamp())
    await db.dm_state(job, "sending", channel_id=200)
    await db.forget(1, 2)

    async def history(**kwargs):
        yield SimpleNamespace(
            id=700,
            created_at=NOW,
            author=SimpleNamespace(id=9),
            embeds=[SimpleNamespace(footer=SimpleNamespace(text=f"Birthday DM {job['marker']}"))],
        )

    partial = scheduler.bot.get_partial_messageable.return_value
    partial.history = history
    partial.get_partial_message.return_value.delete = AsyncMock()
    restarted = Scheduler(scheduler.bot)
    await restarted.tick(NOW + timedelta(days=2))
    await restarted.cleanup_messages(NOW + timedelta(days=2))
    partial.get_partial_message.return_value.delete.assert_awaited_once()
    dm.send.assert_not_awaited()
    channel.send.assert_not_awaited()
    assert (await db.dm_job(1, 2, 2026))["state"] == "sent"
    assert not await db.read("SELECT * FROM cleanup")


async def test_midnight_uses_members_local_year_at_date_line(db):
    scheduler, guild, channel = await fixture(db)
    await db.configure(1, 100, None, "UTC", 0, 0, "")
    await db.save(1, 2, 1, 1, "Pacific/Kiritimati", 0, 0, "feb28", True)
    target = datetime(2025, 12, 31, 10, tzinfo=UTC)
    dm = guild.fetch_member.return_value.create_dm.return_value
    await scheduler.tick(target - timedelta(seconds=1))
    dm.send.assert_not_awaited()
    await scheduler.tick(target)
    dm.send.assert_awaited_once()
    assert await db.dm_job(1, 2, 2025) is None
    assert (await db.dm_job(1, 2, 2026))["state"] == "sent"


async def test_two_servers_get_separate_named_greetings(db):
    scheduler, first, channel = await fixture(db)
    second = MagicMock()
    second.id, second.name = 2, "Other server"
    second.fetch_member = first.fetch_member
    other_channel = MagicMock(spec=discord.TextChannel)
    other_channel.guild.id = 2
    other_channel.id = 101
    other_channel.permissions_for.return_value = discord.Permissions.all()
    other_channel.send = AsyncMock(return_value=SimpleNamespace(id=501))
    second.get_channel.return_value = other_channel
    scheduler.bot.get_guild.side_effect = lambda guild_id: {1: first, 2: second}.get(guild_id)
    await db.ensure_guild(2)
    await db.configure(2, 101, None, "UTC", 9, 0, "")
    await db.save(2, 2, 11, 10, "America/New_York", 9, 0, "feb28", True)
    dm = first.fetch_member.return_value.create_dm.return_value
    dm.send.side_effect = [SimpleNamespace(id=700), SimpleNamespace(id=701)]
    await scheduler.tick(NOW)
    descriptions = [call.kwargs["embed"].description for call in dm.send.call_args_list]
    assert len(descriptions) == 2
    assert "Test server" in descriptions[0] and "Other server" in descriptions[1]


def test_server_name_cannot_inject_mentions_or_markdown_links():
    embed = greeting("@everyone **[click](https://evil.example)**", "", "marker")
    assert "@everyone" not in embed.description
    assert not re.search(r"(?<!\\)\[click\]\(", embed.description)


async def test_retimed_pending_dm_follows_new_announcement_time(db):
    scheduler, guild, channel = await fixture(db)
    member = guild.fetch_member.return_value
    member.create_dm.side_effect = OSError()
    await scheduler.dms.tick(NOW)
    await db.configure(1, 100, None, "UTC", 16, 0, "")
    await scheduler.dms.tick(NOW)
    job = await db.dm_job(1, 2, 2026)
    assert job["state"] == "ready" and job["retry_at"] > NOW.timestamp()
    member.create_dm.side_effect = None
    await db.configure(1, 100, None, "UTC", 10, 0, "")
    await scheduler.dms.tick(NOW)
    member.create_dm.return_value.send.assert_awaited_once()


async def test_already_announced_birthday_can_receive_new_dm_feature(db):
    scheduler, guild, channel = await fixture(db)
    job = await db.claim(await db.birthday(1, 2), 2026, 100, NOW.timestamp())
    await db.delivery_state(job, "sent")
    await scheduler.tick(NOW)
    channel.send.assert_not_awaited()
    guild.fetch_member.return_value.create_dm.return_value.send.assert_awaited_once()


@pytest.mark.parametrize("change", ["optout", "date"])
async def test_profile_changes_cancel_unsent_dm_retries(db, change):
    scheduler, guild, channel = await fixture(db)
    member = guild.fetch_member.return_value
    member.create_dm.side_effect = OSError()
    await scheduler.dms.tick(NOW)
    assert (await db.dm_job(1, 2, 2026))["state"] == "retry"
    await db.save(
        1,
        2,
        11,
        11 if change == "date" else 10,
        "America/New_York",
        9,
        0,
        "feb28",
        change != "optout",
    )
    member.create_dm.side_effect = None
    await Scheduler(scheduler.bot).tick(NOW + timedelta(minutes=2))
    member.create_dm.return_value.send.assert_not_awaited()
    assert await db.dm_job(1, 2, 2026) is None


async def test_dm_batches_are_bounded(db):
    scheduler, guild, channel = await fixture(db)
    await db.forget(1, 2)
    members = {}
    for user in range(60):
        await db.save(1, 100 + user, 11, 10, "America/New_York", 9, 0, "feb28", True)
        dm = SimpleNamespace(
            id=200 + user, send=AsyncMock(return_value=SimpleNamespace(id=700 + user))
        )
        members[100 + user] = SimpleNamespace(
            id=100 + user, bot=False, create_dm=AsyncMock(return_value=dm)
        )
    guild.fetch_member.side_effect = lambda user: members[user]
    assert await scheduler.dms.tick(NOW)
    assert guild.fetch_member.await_count == 25
    assert await scheduler.dms.tick(NOW)
    assert guild.fetch_member.await_count == 50
    assert not await scheduler.dms.tick(NOW)
    assert guild.fetch_member.await_count == 60
