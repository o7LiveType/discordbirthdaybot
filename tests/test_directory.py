import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from birthdaybot.bot import BirthdayBot
from birthdaybot.config import Config
from birthdaybot.database import Database
from birthdaybot.directory import DirectoryView, open_directory


async def seed(db):
    for user, month, day, consent in [
        (10, 12, 25, True),
        (11, 1, 1, True),
        (12, 1, 1, True),
        (13, 2, 29, True),
        (14, 11, 9, False),
    ]:
        await db.save(1, user, month, day, "America/New_York", 9, 0, "feb28", consent)
    await db.ensure_guild(2)
    await db.save(2, 99, 3, 3, "UTC", 9, 0, "feb28", True)


def environment(db, *, mod=False):
    bot = BirthdayBot(Config("test"))
    bot.db = db
    value = MagicMock()
    value.guild_id = 1
    value.channel_id = 100
    value.guild.id = 1
    value.user = MagicMock(spec=discord.Member)
    value.user.id = 20
    value.user.guild_permissions = discord.Permissions(manage_guild=mod)
    value.response.is_done.return_value = True
    value.response.send_message = AsyncMock()
    value.followup.send = AsyncMock()
    value.edit_original_response = AsyncMock()
    value.channel = MagicMock(spec=discord.TextChannel)
    value.channel.id = 100
    value.channel.permissions_for.return_value = discord.Permissions.all()
    message = SimpleNamespace(
        id=1000, created_at=datetime.now(UTC), jump_url="https://discord.com/channels/1/100/1000"
    )
    value.channel.send = AsyncMock(return_value=message)
    value.channel.get_partial_message.return_value.edit = AsyncMock(return_value=message)
    return bot, value


async def test_public_counts_order_month_filter_and_optout_privacy(db):
    await seed(db)
    result = await db.directory(1)
    assert result["configured"] == 5 and result["visible"] == 4
    assert result["months"] == {1: 2, 2: 1, 12: 1}
    assert [r["user_id"] for r in result["rows"]] == [11, 12, 13, 10]
    assert [r["user_id"] for r in (await db.directory(1, month=2))["rows"]] == [13]
    assert (await db.directory(1, month=11))["rows"] == []
    assert (await db.directory(1, public_only=False))["visible"] == 5
    assert (await db.directory(2))["configured"] == 1
    with pytest.raises(ValueError):
        await db.directory(1, month=13)


async def test_real_pagination_is_bounded_and_clamps_after_deletion(db):
    for user in range(25):
        await db.save(1, user, 1, user + 1, "UTC", 9, 0, "feb28", True)
    result = await db.directory(1, page=1)
    assert len(result["rows"]) == 10 and result["pages"] == 3
    assert [row["day"] for row in result["rows"]] == list(range(11, 21))
    for user in range(10, 25):
        await db.forget(1, user)
    result = await db.directory(1, page=2)
    assert result["page"] == 0 and result["pages"] == 1


async def test_public_embed_has_only_dates_and_monthly_summary_hides_optouts(db):
    await seed(db)
    bot, interaction = environment(db)
    view = DirectoryView(bot, 20, 1)
    public = (await view.render())["embed"].to_dict()
    encoded = json.dumps(public)
    assert "America/New_York" not in encoded and "09:00" not in encoded
    assert "<@14>" not in encoded and "<@99>" not in encoded
    assert "<@11>" in encoded
    view.summary = True
    summary = (await view.render())["embed"]
    assert len(summary.fields) == 12
    assert summary.fields[0].name == "January" and summary.fields[0].value == "2"
    assert summary.fields[10].value == "0"
    assert len(view.to_components()) == 3
    assert len(view.children[0].options) == 13


async def test_public_message_is_reused_instead_of_flooding_channel(db):
    await seed(db)
    bot, interaction = environment(db)
    await open_directory(bot, interaction)
    await open_directory(bot, interaction)
    interaction.channel.send.assert_awaited_once()
    interaction.channel.get_partial_message.return_value.edit.assert_not_awaited()
    bot.directory_refreshes[(1, 100)] = time.monotonic() - 5
    await open_directory(bot, interaction, summary=True)
    interaction.channel.send.assert_awaited_once()
    interaction.channel.get_partial_message.return_value.edit.assert_awaited_once()
    assert len(await db.read("SELECT * FROM directory_messages")) == 1
    assert len(await db.read("SELECT * FROM cleanup")) == 1
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    assert not interaction.channel.send.call_args.kwargs["allowed_mentions"].users


async def test_private_policy_blocks_members_but_allows_ephemeral_moderator_view(db):
    await seed(db)
    await db.configure(1, None, None, "UTC", 9, 0, "", public_birthdays=False)
    bot, member = environment(db)
    await open_directory(bot, member)
    member.channel.send.assert_not_awaited()
    assert "restricted" in member.followup.send.call_args.args[0]
    bot, mod = environment(db, mod=True)
    await open_directory(bot, mod)
    mod.channel.send.assert_not_awaited()
    assert mod.followup.send.call_args.kwargs["ephemeral"]
    assert "<@14>" in json.dumps(mod.followup.send.call_args.kwargs["embed"].to_dict())
    mod.user.guild_permissions = discord.Permissions.none()
    assert not await bot.active_views[(1, 20)].interaction_check(mod)


@pytest.mark.parametrize("change", ["optout", "delete", "private"])
async def test_existing_public_snapshots_are_invalidated_and_queued_for_removal(db, change):
    await seed(db)
    bot, interaction = environment(db)
    await open_directory(bot, interaction)
    view = bot.directory_views[(1, 100)]
    if change == "optout":
        await db.save(1, 11, 1, 1, "UTC", 9, 0, "feb28", False)
    elif change == "delete":
        await db.forget(1, 11)
    else:
        await db.configure(1, None, None, "UTC", 9, 0, "", public_birthdays=False)
    assert await db.directory_message(1, 100) is None
    row = (await db.read("SELECT * FROM cleanup"))[0]
    assert row["delete_at"] <= datetime.now(UTC).timestamp()
    assert not await view.interaction_check(interaction)
    assert interaction.edit_original_response.call_args.kwargs["embed"] is None
    if change == "private":
        assert (await view.render())["embed"] is None


async def test_main_menu_navigation_does_not_requeue_itself(db):
    bot, interaction = environment(db)
    await bot.tree.get_command("birthdaybot").callback.__wrapped__(interaction)
    view = bot.active_views[(1, 20)]
    assert {b.label for b in view.children} == {
        "View birthdays",
        "Monthly summary",
        "Set birthday",
        "Update birthday",
        "My birthday",
        "Delete birthday",
        "Sharing",
    }
    bot.queue.start()
    try:
        await next(b for b in view.children if b.label == "Set birthday").callback(interaction)
        assert interaction.edit_original_response.call_args.kwargs["content"] is None
        assert interaction.edit_original_response.call_args.kwargs["embed"].title == "Your birthday"
        assert not bot.queue.pending
    finally:
        await bot.queue.close()


async def test_v3_migration_keeps_records_and_defaults_to_public(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    # A pre-directory guild schema, as deployed in version 3.
    conn.executescript("""
    CREATE TABLE guilds(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,alert_channel_id INTEGER,
      timezone TEXT NOT NULL DEFAULT 'UTC',hour INTEGER NOT NULL DEFAULT 9,minute INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1,gif TEXT NOT NULL DEFAULT '',leap TEXT NOT NULL DEFAULT 'feb28');
    INSERT INTO guilds(guild_id) VALUES(1);
    CREATE TABLE birthdays(guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),user_id INTEGER NOT NULL,
      month INTEGER NOT NULL,day INTEGER NOT NULL,timezone TEXT NOT NULL,hour INTEGER NOT NULL,
      minute INTEGER NOT NULL,leap TEXT NOT NULL,consent INTEGER NOT NULL,updated_at REAL NOT NULL,
      PRIMARY KEY(guild_id,user_id));
    INSERT INTO birthdays VALUES(1,10,2,29,'UTC',9,0,'feb28',0,0);
    PRAGMA user_version=3;
    """)
    conn.close()
    db = Database(path)
    await db.open()
    try:
        assert (await db.guild(1))["public_birthdays"] == 1
        assert (await db.birthday(1, 10))["day"] == 29
        assert (await db.birthday(1, 10))["consent"] == 0
        await db.configure(1, None, None, "UTC", 9, 0, "", public_birthdays=False)
    finally:
        await db.close()
    reopened = Database(path)
    await reopened.open()
    try:
        assert (await reopened.guild(1))["public_birthdays"] == 0
        assert (await reopened.birthday(1, 10))["consent"] == 0
    finally:
        await reopened.close()


async def test_public_calendar_queued_controls_work_for_a_different_member(db):
    await seed(db)
    bot, interaction = environment(db)
    await open_directory(bot, interaction)
    view = bot.directory_views[(1, 100)]
    interaction.user.id = 21  # Calendar owner is 20; shared controls allow members.
    assert await view.interaction_check(interaction)
    bot.queue.start()
    try:
        await next(
            item for item in view.children if getattr(item, "label", None) == "Monthly summary"
        ).callback(interaction)
        assert view.summary
        assert (
            interaction.edit_original_response.call_args.kwargs["embed"].title
            == "Birthdays by month"
        )
    finally:
        await bot.queue.close()
