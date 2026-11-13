import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from birthdaybot.bot import BirthdayBot
from birthdaybot.commands import require_mod
from birthdaybot.config import Config, gif_url
from birthdaybot.logging_setup import JsonFormatter
from birthdaybot.ui import BirthdayWizard, FieldsModal, OwnedView, SettingsWizard


@pytest.mark.parametrize(
    "url",
    [
        "http://static.klipy.com/x.gif",
        "https://127.0.0.1/x.gif",
        "https://evil.com/x.gif",
        "https://static.klipy.com.evil.com/a.gif",
        "https://static.klipy.com/x.gif?token=secret",
        "https://user:pass@static.klipy.com/x.gif",
        "https://static.klipy.com:8443/x.gif",
        "file:///tmp/x.gif",
        "https://static.klipy.com/x.mp4",
        "https://static.klipy.com/a.gif\n@everyone/b.gif",
        "https://static.klipy.com/%0Amessage.gif",
        "https://static.klipy.com/%00.gif",
        "https://static.klipy.com/%5Cevil.gif",
        "https://static.klipy.com/bad%ZZ.gif",
        "https://@static.klipy.com/a.gif",
        "https://static.klipy.com:bad/a.gif",
        "https://static.klipy.com/" + "a" * 2048 + ".gif",
    ],
)
def test_external_urls_rejected(url):
    with pytest.raises(ValueError):
        gif_url(url)


def interaction(mod=False, user=10):
    value = MagicMock()
    value.guild = MagicMock()
    value.guild_id = 1
    value.user = MagicMock(spec=discord.Member)
    value.user.id = user
    value.user.guild_permissions = discord.Permissions(manage_guild=mod)
    value.response.is_done.return_value = False
    value.response.send_message = AsyncMock()
    value.response.defer = AsyncMock()
    value.response.edit_message = AsyncMock()
    value.edit_original_response = AsyncMock()
    value.followup.send = AsyncMock()
    return value


def test_moderator_runtime_check_does_not_trust_command_visibility():
    with pytest.raises(app_commands.MissingPermissions):
        require_mod(interaction())
    assert require_mod(interaction(True))


async def test_private_menu_owner_and_revoked_moderator_access():
    view = OwnedView(10, admin=True)
    other = interaction(True, user=11)
    assert not await view.interaction_check(other)
    assert other.response.send_message.call_args.kwargs["ephemeral"] is True
    revoked = interaction(False)
    assert not await view.interaction_check(revoked)
    assert await view.interaction_check(interaction(True))


async def test_actual_command_callbacks_reject_every_admin_action():
    bot = BirthdayBot(Config(token="test"))
    assert [command.name for command in bot.tree.get_commands()] == ["birthdaybot"]
    assert not bot.tree.get_command("birthdaybot").parameters
    assert bot.admin_actions == {
        "tools",
        "setup",
        "records",
        "forget",
        "pause",
        "preview",
        "status",
        "channel_deliveries",
        "dm_deliveries",
        "import",
    }
    for name in bot.admin_actions:
        with pytest.raises(app_commands.MissingPermissions):
            await bot.menu_actions[name](interaction())
    assert not bot.intents.members
    assert not bot.intents.message_content
    assert not bot.intents.presences


async def test_menus_fit_discord_limits_and_preserve_opt_out(db):
    settings = await db.guild(1)
    view = BirthdayWizard(MagicMock(), 10, 1, settings)
    assert not view.opt_out
    assert len(view.to_components()) == 5
    view.month, view.day = 2, 29
    view.build()
    for component in view.children:
        if isinstance(component, discord.ui.Select):
            assert len(component.options) <= 25
    admin_view = SettingsWizard(MagicMock(), 10, settings)
    assert len(admin_view.to_components()) == 2
    assert {option.label for option in admin_view.children[0].options} >= {
        "Announcement time",
        "Birthday GIF link",
    }
    modal = FieldsModal("Test", [("zone", "Timezone", "UTC")], AsyncMock(), 10)
    assert modal.to_components()[0]["type"] == 18  # Modern Label, not a legacy ActionRow.


async def test_update_preserves_optout_unless_explicitly_changed(db):
    await db.save(1, 10, 11, 10, "UTC", 9, 0, "feb28", False)
    settings = await db.guild(1)
    record = await db.birthday(1, 10)
    assert BirthdayWizard(MagicMock(), 10, 1, settings, record, update=True).opt_out
    assert not BirthdayWizard(
        MagicMock(), 10, 1, settings, record, opt_out=False, update=True
    ).opt_out


async def test_save_is_private_uses_current_moderator_settings_and_keeps_optout(db):
    bot = SimpleNamespace(db=db, queue=None, mutation_lock=asyncio.Lock())
    view = BirthdayWizard(bot, 10, 1, await db.guild(1), opt_out=True)
    view.month, view.day, view.zone = 11, 10, "America/New_York"
    await db.configure(1, 100, None, "UTC", 10, 30, "", "mar1")
    value = interaction()
    value.response.defer = AsyncMock()
    value.edit_original_response = AsyncMock()
    await view.save(value)
    record = await db.birthday(1, 10)
    assert record["consent"] == 0
    assert (record["hour"], record["minute"], record["leap"]) == (10, 30, "mar1")
    assert value.response.defer.call_args.kwargs["ephemeral"]
    assert value.edit_original_response.call_args.kwargs["view"] is None
    assert "opted out" in value.edit_original_response.call_args.kwargs["content"]


def test_logs_redact_bot_token_and_authorization():
    formatter = JsonFormatter("super-secret-token")
    record = logging.LogRecord(
        "test", logging.INFO, "", 1, "token=super-secret-token Bot abc.def Bearer xyz", (), None
    )
    output = formatter.format(record)
    assert "super-secret-token" not in output
    assert "abc.def" not in output
    assert "Bearer xyz" not in output


def test_gif_url_canonicalizes_markup_and_cannot_break_a_link():
    value = gif_url("HTTPS://STATIC.KLIPY.COM:443/a](evil)`<x>.gif")
    assert value == "https://static.klipy.com/a%5D%28evil%29%60%3Cx%3E.gif"
    assert not any(char in value for char in "[]()`<>")


async def test_time_selectors_and_gif_form_save_and_validate(db):
    from birthdaybot.work_queue import WorkQueue

    queue = WorkQueue()
    bot = SimpleNamespace(db=db, queue=queue, mutation_lock=asyncio.Lock(), config=Config("test"))
    view = SettingsWizard(bot, 10, await db.guild(1))
    value = interaction(mod=True)
    value.response.send_modal = AsyncMock()
    value.response.edit_message = AsyncMock()
    value.response.is_done.return_value = False
    value.edit_original_response = AsyncMock()

    queue.start()
    try:

        async def select(row, choice):
            menu = next(
                item
                for item in view.children
                if isinstance(item, discord.ui.Select) and item.row == row
            )
            menu._values = [choice]
            await menu.callback(value)

        await select(0, "time")
        value.response.send_modal.assert_not_awaited()
        assert len(view.to_components()) == 5
        await select(1, "18")
        await select(2, "2")
        await select(3, "45")
        assert (await db.guild(1))["hour"] == 0  # Draft only until Save.
        assert (view.hour, view.minute) == (18, 45)
        await view.reset_midnight(value)
        assert (view.hour, view.minute) == (0, 0)

        # GIF is one of the two necessary text inputs. It must open as the
        # initial response, before the queue can defer the interaction.
        await select(0, "gif")
        modal = value.response.send_modal.call_args.args[0]
        with pytest.raises(ValueError):
            await modal.submit(value, {"gif": "https://evil.example/image.gif"})
        assert view.gif == ""
        await modal.submit(value, {"gif": "https://static.klipy.com/birthday(cake).gif"})
        assert view.gif == "https://static.klipy.com/birthday%28cake%29.gif"
        assert f"[Open GIF]({view.gif})" in view.content()
        assert (await db.guild(1))["gif"] == ""

        await select(0, "time")
        await select(1, "18")
        await select(2, "2")
        await select(3, "45")
        value.response.is_done.return_value = True
        await view.save(value)
        saved = await db.guild(1)
        assert (saved["hour"], saved["minute"], saved["gif"]) == (18, 45, view.gif)
    finally:
        await queue.close()
