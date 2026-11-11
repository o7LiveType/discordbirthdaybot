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
    ],
)
def test_external_urls_rejected(url):
    with pytest.raises(ValueError):
        gif_url(url)


def interaction(mod=False, user=10):
    value = MagicMock()
    value.guild = MagicMock()
    value.user = MagicMock(spec=discord.Member)
    value.user.id = user
    value.user.guild_permissions = discord.Permissions(manage_guild=mod)
    value.response.is_done.return_value = False
    value.response.send_message = AsyncMock()
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
    admin = bot.tree.get_command("birthday-admin")
    assert admin.default_permissions.manage_guild
    for command in admin.commands:
        assert command.checks, command.name
        with pytest.raises(app_commands.MissingPermissions):
            for check in command.checks:
                check(interaction())
    assert not bot.intents.members
    assert not bot.intents.message_content
    assert not bot.intents.presences
    assert {c.name for c in bot.tree.get_command("birthday").commands} == {
        "set",
        "update",
        "me",
        "delete",
        "help",
        "list",
        "summary",
    }
    assert not bot.tree.get_command("birthday").get_command("me").parameters
    for name in ("set", "update"):
        params = bot.tree.get_command("birthday").get_command(name).parameters
        assert [p.name for p in params] == ["opt_out", "timezone"]
        assert not any(p.required for p in params)


async def test_menus_fit_discord_limits_and_preserve_opt_out(db):
    settings = await db.guild(1)
    view = BirthdayWizard(MagicMock(), 10, 1, settings)
    assert not view.opt_out
    assert len(view.to_components()) == 4
    view.month, view.day = 2, 29
    view.build()
    for component in view.children:
        if isinstance(component, discord.ui.Select):
            assert len(component.options) <= 25
    admin_view = SettingsWizard(MagicMock(), 10, settings)
    assert len(admin_view.to_components()) == 5
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
    view.month, view.day, view.zone = 11, 10, "America/Denver"
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
