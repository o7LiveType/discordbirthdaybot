import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from birthdaybot.delivery import Scheduler

NOW = datetime(2026, 11, 10, 17, tzinfo=UTC)


async def fixture(db):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 100
    channel.guild.id = 1
    channel.permissions_for.return_value = discord.Permissions.all()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=500))
    member = SimpleNamespace(id=2, mention="<@2>", bot=False)
    guild = MagicMock()
    guild.id = 1
    guild.get_channel.return_value = channel
    guild.fetch_member = AsyncMock(return_value=member)
    bot = MagicMock()
    bot.db = db
    bot.get_guild.return_value = guild
    bot.get_channel.side_effect = lambda channel_id: channel if channel_id == 100 else None
    bot.mutation_lock = asyncio.Lock()
    bot.config.gif = "https://static.klipy.com/test.gif"
    bot.user.id = 9
    bot.latency = 0.1
    bot.live_clock = False
    await db.configure(1, 100, None, "America/Denver", 9, 0, "")
    await db.save(1, 2, 11, 10, "America/Denver", 9, 0, "feb28", True)
    return Scheduler(bot), guild, channel


async def test_send_once_across_repeat_ticks_and_restart(db):
    scheduler, guild, channel = await fixture(db)
    await scheduler.tick(NOW)
    await scheduler.tick(NOW + timedelta(seconds=30))
    scheduler = Scheduler(scheduler.bot)
    await scheduler.tick(NOW + timedelta(seconds=60))
    assert channel.send.await_count == 1
    assert (await db.read("SELECT state FROM deliveries"))[0][0] == "sent"
    mentions = channel.send.call_args.kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.roles
    assert [u.id for u in mentions.users] == [2]
    assert channel.send.call_args.kwargs["nonce"]


@pytest.mark.parametrize("change", ["optout", "paused", "future", "yesterday", "wrong_guild"])
async def test_no_unauthorized_or_wrong_day_posts(db, change):
    scheduler, guild, channel = await fixture(db)
    if change == "optout":
        await db.save(1, 2, 11, 10, "America/Denver", 9, 0, "feb28", False)
    elif change == "paused":
        await db.pause(1, True)
    elif change == "wrong_guild":
        scheduler.bot.get_guild.return_value = None
    now = NOW + timedelta(days=1) if change == "yesterday" else NOW
    now = NOW - timedelta(hours=2) if change == "future" else now
    await scheduler.tick(now)
    channel.send.assert_not_awaited()


def empty_history(**kwargs):
    async def iterate():
        for _ in []:
            yield

    return iterate()


async def test_ambiguous_network_error_is_not_automatically_resent(db):
    scheduler, guild, channel = await fixture(db)
    channel.send.side_effect = TimeoutError()
    channel.history = empty_history
    await scheduler.tick(NOW)
    await scheduler.tick(NOW + timedelta(seconds=30))
    assert channel.send.await_count == 1
    assert (await db.read("SELECT state FROM deliveries"))[0][0] == "uncertain"


async def test_reconcile_after_crash_between_send_and_commit(db):
    scheduler, guild, channel = await fixture(db)
    job = await db.claim(await db.birthday(1, 2), 2026, 100, NOW.timestamp())
    await db.delivery_state(job, "sending")

    async def history(**kwargs):
        yield SimpleNamespace(
            id=600,
            author=SimpleNamespace(id=9),
            embeds=[
                SimpleNamespace(footer=SimpleNamespace(text=f"Birthday delivery {job['marker']}"))
            ],
        )

    channel.history = history
    await scheduler.tick(NOW)
    channel.send.assert_not_awaited()
    row = (await db.read("SELECT * FROM deliveries"))[0]
    assert row["state"] == "sent" and row["message_id"] == 600


async def test_safe_failure_retries_and_success_is_durable(db):
    scheduler, guild, channel = await fixture(db)
    rejected = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied")
    channel.send.side_effect = [rejected, SimpleNamespace(id=501)]
    await scheduler.tick(NOW)
    assert (await db.read("SELECT state FROM deliveries"))[0][0] == "retry"
    await scheduler.tick(NOW + timedelta(seconds=30))
    assert channel.send.await_count == 1
    await scheduler.tick(NOW + timedelta(hours=1))
    assert (await db.read("SELECT state FROM deliveries"))[0][0] == "sent"


async def test_departed_member_is_erased_without_post(db):
    scheduler, guild, channel = await fixture(db)
    guild.fetch_member.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), "Unknown member"
    )
    await scheduler.tick(NOW)
    channel.send.assert_not_awaited()
    assert await db.birthday(1, 2) is None


async def test_retry_channel_change_records_actual_destination_for_recovery(db):
    scheduler, guild, channel = await fixture(db)
    job = await db.claim(await db.birthday(1, 2), 2026, 99, NOW.timestamp())
    await db.delivery_state(job, "retry")
    channel.send.side_effect = TimeoutError()
    channel.history = empty_history
    await scheduler.tick(NOW)
    assert (await db.read("SELECT channel_id FROM deliveries"))[0][0] == 100


async def test_midnight_crossing_during_member_lookup_prevents_late_send(db):
    scheduler, guild, channel = await fixture(db)
    scheduler.bot.live_clock = True
    with patch("birthdaybot.delivery.datetime") as mocked:
        mocked.now.side_effect = [NOW, NOW + timedelta(days=1)]
        await scheduler.tick(NOW)
    channel.send.assert_not_awaited()


async def test_post_expires_after_24_hours_even_after_record_deletion_and_restart(db):
    scheduler, guild, channel = await fixture(db)
    await scheduler.tick(NOW)
    message = SimpleNamespace(delete=AsyncMock())
    scheduler.bot.get_partial_messageable.return_value.get_partial_message.return_value = message
    await db.forget(1, 2)
    assert len(await db.read("SELECT * FROM cleanup")) == 1
    restarted = Scheduler(scheduler.bot)
    await restarted.tick(NOW + timedelta(hours=23, minutes=59))
    message.delete.assert_not_awaited()
    await restarted.tick(NOW + timedelta(hours=24))
    message.delete.assert_awaited_once()
    assert not await db.read("SELECT * FROM cleanup")


async def test_failed_cleanup_retries_durably_and_already_deleted_is_success(db):
    scheduler, guild, channel = await fixture(db)
    await scheduler.tick(NOW)
    delete = AsyncMock(
        side_effect=[
            discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied"),
            discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "Gone"),
        ]
    )
    scheduler.bot.get_partial_messageable.return_value.get_partial_message.return_value.delete = (
        delete
    )
    await scheduler.cleanup_messages(NOW + timedelta(hours=24))
    assert (await db.read("SELECT attempts FROM cleanup"))[0][0] == 1
    await Scheduler(scheduler.bot).cleanup_messages(NOW + timedelta(hours=24, minutes=2))
    assert not await db.read("SELECT * FROM cleanup")
