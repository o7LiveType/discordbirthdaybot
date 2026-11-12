"""Exercise menu paths without a Discord token or network calls."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from birthdaybot.bot import BirthdayBot
from birthdaybot.commands import DeliveryList, DeliveryReview
from birthdaybot.config import Config
from birthdaybot.delivery import Scheduler
from birthdaybot.ui import BirthdayWizard, FieldsModal, SettingsWizard


@pytest.fixture
async def menu(db):
    bot = BirthdayBot(Config("test"))
    bot.db = db
    bot.scheduler = Scheduler(bot)
    bot._connection.user = SimpleNamespace(id=900)
    value = MagicMock()
    value.guild_id, value.channel_id = 1, 100
    value.user = MagicMock(spec=discord.Member)
    value.user.id = 20
    value.user.guild_permissions = discord.Permissions.none()
    value.response.is_done.return_value = True
    value.response.send_message = AsyncMock()
    value.response.send_modal = AsyncMock()
    value.followup.send = AsyncMock()
    value.edit_original_response = AsyncMock()
    bot.queue.start()
    try:
        yield bot, value
    finally:
        await bot.queue.close()
        for view in bot.active_views.values():
            view.stop()


async def click(bot, value, label):
    view = bot.active_views[(1, 20)]
    await next(item for item in view.children if getattr(item, "label", None) == label).callback(
        value
    )


async def choose(view, value, row, selected):
    item = next(
        item for item in view.children if isinstance(item, discord.ui.Select) and item.row == row
    )
    item._values = [selected]
    await item.callback(value)


async def test_member_set_optout_update_and_delete_entirely_by_menu(menu):
    bot, value = menu
    await bot.tree.get_command("birthdaybot").callback(value)
    await click(bot, value, "Set birthday")
    view = bot.active_views[(1, 20)]
    assert isinstance(view, BirthdayWizard) and view.zone == "UTC"
    assert "UTC+00:00" in view.content()
    await choose(view, value, 0, "2")
    await choose(view, value, 1, "page")
    await choose(view, value, 1, "29")
    await click(bot, value, "Sharing: ON")
    await click(bot, value, "Save")
    assert (await bot.db.birthday(1, 20))["consent"] == 0
    assert "opted out" in value.edit_original_response.call_args.kwargs["content"]
    await click(bot, value, "Main menu")
    await click(bot, value, "Update birthday")
    assert bot.active_views[(1, 20)].opt_out
    await click(bot, value, "Sharing: OFF")
    await click(bot, value, "Save")
    assert (await bot.db.birthday(1, 20))["consent"] == 1
    await click(bot, value, "Main menu")
    await click(bot, value, "Delete birthday")
    assert await bot.db.birthday(1, 20)  # No deletion before confirmation.
    await click(bot, value, "Confirm deletion")
    assert await bot.db.birthday(1, 20) is None
    assert not bot.queue.pending


async def test_timezone_search_errors_ambiguity_and_canonical_confirmation(menu):
    bot, value = menu
    await bot.menu_actions["set"](value)
    view = bot.active_views[(1, 20)]
    view.month, view.day = 1, 1
    await view.search_zone(value, "@everyone\n<unsafe>")
    assert "❌" in view.content() and "@everyone" not in view.content()
    assert next(
        item for item in view.children if getattr(item, "label", None) == "Use this timezone"
    ).disabled
    assert view.zone == "UTC"  # A failed search cannot destroy the birthday draft.
    with pytest.raises(ValueError, match="valid timezone"):
        await view.zone_browser.confirm(value)
    await view.search_zone(value, "Indiana")
    matches = view.zone_browser.results()
    assert 1 < len(matches) <= 25
    assert "Choose your timezone" in view.content()
    assert len(next(item for item in view.children if item.row == 2).options) <= 25
    await choose(view, value, 2, matches[0].name)
    assert view.zone_browser.selected == matches[0].name
    await view.search_zone(value, "new york")
    assert view.zone_browser.selected == "America/New_York" and view.zone == "UTC"
    assert "Valid match" in view.content() and "UTC-" in view.content()
    await click(bot, value, "Use this timezone")
    assert view.zone == "America/New_York" and not view.browsing_zone
    await view.save(value)
    assert (await bot.db.birthday(1, 20))["timezone"] == "America/New_York"
    assert "UTC-" in value.edit_original_response.call_args.kwargs["content"]


async def test_other_timezone_modal_opens_before_defer_and_submission_is_queued(menu):
    bot, value = menu
    await bot.menu_actions["set"](value)
    view = bot.active_views[(1, 20)]
    value.response.is_done.return_value = False
    value.response.defer = AsyncMock()
    value.response.edit_message = AsyncMock()
    await choose(view, value, 2, "custom")
    value.response.send_modal.assert_awaited_once()
    value.response.defer.assert_not_awaited()
    modal = value.response.send_modal.call_args.args[0]
    modal.inputs["zone"]._value = "Asia/Kathmandu"
    await modal.on_submit(value)
    value.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    assert "UTC+05:45" in view.content()
    assert not bot.queue.pending
    assert await bot.db.birthday(1, 20) is None


async def test_mod_settings_and_member_deletion_use_selectors(menu):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    await bot.menu_actions["home"](value)
    await click(bot, value, "Server tools")
    await click(bot, value, "Server settings")
    view = bot.active_views[(1, 20)]
    assert isinstance(view, SettingsWizard)
    await choose(view, value, 0, "privacy")
    await choose(view, value, 1, "private")
    assert (await bot.db.guild(1))["public_birthdays"] == 1
    await click(bot, value, "Save settings")
    assert (await bot.db.guild(1))["public_birthdays"] == 0
    await click(bot, value, "Server tools")
    await click(bot, value, "Delete member birthday")
    select = next(
        item
        for item in bot.active_views[(1, 20)].children
        if isinstance(item, discord.ui.UserSelect)
    )
    await bot.db.save(1, 55, 6, 2, "UTC", 0, 0, "feb28", False)
    await bot.db.ensure_guild(2)
    await bot.db.save(2, 55, 6, 2, "UTC", 0, 0, "feb28", False)
    select._values = [SimpleNamespace(id=55)]
    await select.callback(value)
    assert await bot.db.birthday(1, 55)
    await click(bot, value, "Confirm deletion")
    assert await bot.db.birthday(1, 55) is None
    assert await bot.db.birthday(2, 55)


async def test_revoked_mod_access_blocks_queued_mutations_and_modal_submissions(menu):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    await bot.menu_actions["setup"](value)
    view = bot.active_views[(1, 20)]
    value.user.guild_permissions = discord.Permissions.none()
    await next(
        item for item in view.children if getattr(item, "label", None) == "Save settings"
    ).callback(value)
    assert "Manage Server" in value.followup.send.call_args.args[0]
    submit = AsyncMock()
    modal = FieldsModal("GIF", [("gif", "GIF URL", "")], submit, 20, admin=True, queue=bot.queue)
    await modal.on_submit(value)
    submit.assert_not_awaited()


async def seed_job(bot, *, kind="channel", guild=1, user=20, year=2026):
    await bot.db.ensure_guild(guild)
    await bot.db.save(guild, user, 1, 1, "UTC", 0, 0, "feb28", True)
    record = await bot.db.birthday(guild, user)
    if kind == "dm":
        job = await bot.db.claim_dm(record, year, 1000)
        await bot.db.dm_state(job, "uncertain", channel_id=100)
    else:
        job = await bot.db.claim(record, year, 100, 1000)
        await bot.db.delivery_state(job, "uncertain")
    return job


@pytest.mark.parametrize("kind", ["channel", "dm"])
async def test_delivery_selector_and_verified_recovery_schedule_expiry(menu, monkeypatch, kind):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    job = await seed_job(bot, kind=kind)
    other = await seed_job(bot, kind=kind, guild=2, user=55)
    view = DeliveryList(bot, 20, 1, kind)
    assert "<@20>" in await view.render() and "<@55>" not in await view.render()
    select = next(item for item in view.children if isinstance(item, discord.ui.Select))
    assert [option.value for option in select.options] == [job["marker"]]
    select._values = [job["marker"]]
    await select.callback(value)
    review = bot.active_views[(1, 20)]
    assert isinstance(review, DeliveryReview)
    channel = MagicMock()
    footer = "Birthday DM" if kind == "dm" else "Birthday delivery"
    embed = discord.Embed().set_footer(text=f"{footer} {job['marker']}")
    message = SimpleNamespace(
        id=800, author=SimpleNamespace(id=900), embeds=[embed], created_at=datetime.now(UTC)
    )
    wrong_author = SimpleNamespace(id=801, author=SimpleNamespace(id=901), embeds=[embed])
    wrong_marker = SimpleNamespace(
        id=802,
        author=SimpleNamespace(id=900),
        embeds=[discord.Embed().set_footer(text=f"{footer} {other['marker']}")],
    )

    async def history(**kwargs):
        assert kwargs["limit"] == 500
        yield wrong_author
        yield wrong_marker
        yield message

    channel.history = history
    monkeypatch.setattr(bot, "get_partial_messageable", lambda channel_id: channel)
    await click(bot, value, "Find posted message")
    getter = (
        bot.db.dm_job
        if kind == "dm"
        else lambda guild, user, year: bot.db.read(
            "SELECT * FROM deliveries WHERE guild_id=? AND user_id=? AND year=?",
            (guild, user, year),
        )
    )
    stored = await getter(1, 20, 2026)
    if kind == "channel":
        stored = stored[0]
    assert stored["state"] == "sent" and stored["message_id"] == 800
    cleanup = (await bot.db.read("SELECT * FROM cleanup WHERE message_id=800"))[0]
    assert cleanup["delete_at"] == message.created_at.timestamp() + 86400
    with pytest.raises(app_commands.MissingPermissions):
        value.user.guild_permissions = discord.Permissions.none()
        await review.resolve(value, "retry")


@pytest.mark.parametrize("kind", ["channel", "dm"])
async def test_retry_requires_confirmation_and_cannot_overwrite_resolved_delivery(menu, kind):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    job = await seed_job(bot, kind=kind)
    review = DeliveryReview(bot, 20, 1, kind, job["marker"])
    await review.decide(value)
    assert (await review.job())["state"] == "uncertain"
    await click(bot, value, "Checked for duplicates — retry")
    assert (await review.job())["state"] == "retry"
    change = bot.db.dm_state if kind == "dm" else bot.db.delivery_state
    await change(job, "skipped")
    await review.resolve(value, "retry")
    assert (await review.job())["state"] == "skipped"


async def test_expired_timezone_form_cannot_modify_a_replacement_menu(menu):
    bot, value = menu
    await bot.menu_actions["set"](value)
    old = bot.active_views[(1, 20)]
    await choose(old, value, 2, "custom")
    modal = value.response.send_modal.call_args.args[0]
    await bot.menu_actions["home"](value)
    modal.inputs["zone"]._value = "Asia/Tokyo"
    await modal.on_submit(value)
    assert old.zone == "UTC" and old.is_finished()
    assert "no longer have access" in value.followup.send.call_args.args[0]


async def test_long_valid_gif_link_remains_clickable_without_oversized_message(menu):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    settings = await bot.db.guild(1)
    view = SettingsWizard(bot, 20, settings)
    view.gif = "https://static.klipy.com/" + "x" * 1900 + ".gif"
    bot.track_view(view, 1, 20)
    await choose(view, value, 0, "overview")
    payload = value.edit_original_response.call_args.kwargs
    assert payload["content"] is None
    assert f"[Open GIF]({view.gif})" in payload["embed"].description
    assert len(payload["embed"].description) <= 4096
    await click(bot, value, "Save settings")
    assert (await bot.db.guild(1))["gif"] == view.gif
    assert value.edit_original_response.call_args.kwargs["content"] is None
    await click(bot, value, "Main menu")
    assert value.edit_original_response.call_args.kwargs["embed"] is None


async def test_history_search_is_bounded_and_can_continue_older_messages(menu, monkeypatch):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    job = await seed_job(bot)
    review = DeliveryReview(bot, 20, 1, "channel", job["marker"])
    bot.track_view(review, 1, 20)
    channel = MagicMock()
    calls = []

    async def history(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            for message_id in range(2000, 1500, -1):
                yield SimpleNamespace(id=message_id, author=SimpleNamespace(id=901), embeds=[])
        else:
            assert kwargs["before"].id == 1501
            yield SimpleNamespace(id=1500, author=SimpleNamespace(id=901), embeds=[])

    channel.history = history
    monkeypatch.setattr(bot, "get_partial_messageable", lambda channel_id: channel)
    await click(bot, value, "Find posted message")
    assert review.before == 1501
    assert "batch of 500" in value.edit_original_response.call_args.kwargs["content"]
    await click(bot, value, "Search older messages")
    assert review.before is None
    assert "remaining history" in value.edit_original_response.call_args.kwargs["content"]
    assert (await review.job())["state"] == "uncertain"
    assert all(call["limit"] == 500 and call["oldest_first"] is False for call in calls)


async def test_delivery_pagination_does_not_drop_unresolved_records(menu):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    for user in range(30, 42):
        await seed_job(bot, user=user)
    view = DeliveryList(bot, 20, 1, "channel")
    await view.render()
    assert (
        len(next(item for item in view.children if isinstance(item, discord.ui.Select)).options)
        == 10
    )
    await view.next(value)
    assert (
        len(next(item for item in view.children if isinstance(item, discord.ui.Select)).options)
        == 2
    )
    assert next(item for item in view.children if getattr(item, "label", None) == "Next").disabled


async def test_all_announcement_minutes_are_available_by_selectors(menu):
    bot, value = menu
    value.user.guild_permissions = discord.Permissions(manage_guild=True)
    view = SettingsWizard(bot, 20, await bot.db.guild(1))
    view.section = "time"
    minutes = set()
    for band in range(3):
        view.minute_band = band
        view.build()
        assert len(view.to_components()) == 5
        assert (
            len(
                next(
                    item
                    for item in view.children
                    if isinstance(item, discord.ui.Select) and item.row == 1
                ).options
            )
            == 24
        )
        minutes.update(
            option.value
            for option in next(
                item
                for item in view.children
                if isinstance(item, discord.ui.Select) and item.row == 3
            ).options
        )
    assert minutes == {str(minute) for minute in range(60)}


async def test_private_actions_reject_a_different_guild_even_for_same_owner(menu):
    bot, value = menu
    await bot.menu_actions["set"](value)
    view = bot.active_views[(1, 20)]
    value.guild_id = 2
    assert not await view.interaction_check(value)
    await next(
        item for item in view.children if getattr(item, "label", None) == "Sharing: ON"
    ).callback(value)
    assert not view.opt_out


async def test_invalid_timezone_search_can_retry_immediately_without_changing_zone(menu):
    bot, value = menu
    await bot.menu_actions["set"](value)
    view = bot.active_views[(1, 20)]
    view.month, view.day, view.opt_out = 2, 29, True
    value.response.is_done.return_value = False
    value.response.defer = AsyncMock()
    value.response.edit_message = AsyncMock()
    await click(bot, value, "Search timezones")
    value.response.defer.assert_not_awaited()  # Opening a button modal must be first.
    first = value.response.send_modal.call_args.args[0]
    first.inputs["zone"]._value = "<unsafe>"
    await first.on_submit(value)
    assert view.zone == "UTC" and view.browsing_zone
    assert "❌" in view.content()
    assert await bot.db.birthday(1, 20) is None
    value.response.defer.reset_mock()
    await click(bot, value, "Search")  # No timezone dropdown change needed.
    value.response.defer.assert_not_awaited()
    second = value.response.send_modal.call_args.args[0]
    assert second is not first
    second.inputs["zone"]._value = "NoSuchCityAnywhere"
    await second.on_submit(value)
    assert "No matches" in view.content() and view.zone == "UTC"
    await click(bot, value, "Search")
    third = value.response.send_modal.call_args.args[0]
    third.inputs["zone"]._value = "Kathmandu"
    await third.on_submit(value)
    assert "Local clock there" in view.content() and "UTC+05:45" in view.content()
    assert view.zone == "UTC"  # Still needs the explicit Use button.
    await click(bot, value, "Use this timezone")
    assert view.zone == "Asia/Kathmandu" and not view.browsing_zone
    assert (view.month, view.day, view.opt_out) == (2, 29, True)
    assert await bot.db.birthday(1, 20) is None
    await click(bot, value, "Save")
    assert (await bot.db.birthday(1, 20))["timezone"] == "Asia/Kathmandu"


async def test_guided_browser_reaches_every_zone_with_valid_discord_components(menu):
    from birthdaybot.timeutil import ZONES

    bot, value = menu
    await bot.menu_actions["set"](value)
    await click(bot, value, "Browse all timezones")
    view = bot.active_views[(1, 20)]
    visited = set()
    for page in range((len(ZONES) + 24) // 25):
        assert view.zone_browser.page == page
        components = view.to_components()
        assert len(components) <= 5
        for row in components:
            assert len(row["components"]) <= 5
            for item in row["components"]:
                if item["type"] == 3:
                    assert 1 <= len(item["options"]) <= 25
                    assert all(len(option["label"]) <= 100 for option in item["options"])
        cities = next(
            item for item in view.children if isinstance(item, discord.ui.Select) and item.row == 2
        )
        visited.update(option.value for option in cities.options)
        next_button = next(
            item for item in view.children if getattr(item, "label", None) == "Next cities"
        )
        if next_button.disabled:
            break
        await click(bot, value, "Next cities")
    assert visited == set(ZONES)
    assert view.zone == "UTC"


async def test_region_and_offset_filters_are_complete_and_reset_search_errors(menu):
    from birthdaybot.timezone_catalog import catalog

    bot, value = menu
    await bot.menu_actions["set"](value)
    await click(bot, value, "Browse all timezones")
    view = bot.active_views[(1, 20)]
    await view.search_zone(value, "<invalid>")
    await choose(view, value, 0, "America")
    assert view.zone_browser.results()
    assert all(entry.region == "America" for entry in view.zone_browser.results())
    # Every current offset remains reachable despite Discord's 25-option limit.
    offsets = set()
    while True:
        menu_item = next(
            item for item in view.children if isinstance(item, discord.ui.Select) and item.row == 1
        )
        offsets.update(
            int(option.value)
            for option in menu_item.options
            if option.value not in {"all", "next", "previous"}
        )
        if not any(option.value == "next" for option in menu_item.options):
            break
        await choose(view, value, 1, "next")
    assert offsets == {entry.offset for entry in catalog()}
    await choose(view, value, 1, "345")  # No Americas city currently has this offset.
    assert not view.zone_browser.results()
    assert "0 matches" in view.content()
    await click(bot, value, "Reset filters")
    assert len(view.zone_browser.results()) == len(catalog())
    await click(bot, value, "Back to birthday")
    assert view.zone == "UTC" and not view.browsing_zone


async def test_region_offset_search_results_require_an_iana_city_choice(menu):
    bot, value = menu
    await bot.menu_actions["set"](value)
    view = bot.active_views[(1, 20)]
    await view.search_zone(value, "Europe")
    assert len(view.zone_browser.results()) > 25
    assert view.zone_browser.selected is None
    await choose(view, value, 2, "Europe/Amsterdam")
    assert view.zone_browser.selected == "Europe/Amsterdam"
    await click(bot, value, "Use this timezone")
    assert view.zone == "Europe/Amsterdam"
    await click(bot, value, "Browse all timezones")
    await choose(view, value, 2, "Africa/Abidjan")
    await click(bot, value, "Back to birthday")
    assert view.zone == "Europe/Amsterdam"  # Back discards the unconfirmed candidate.


async def test_search_button_cannot_open_for_wrong_owner_or_expired_menu(menu):
    bot, value = menu
    await bot.menu_actions["set"](value)
    view = bot.active_views[(1, 20)]
    search = next(
        item for item in view.children if getattr(item, "label", None) == "Search timezones"
    )
    value.user.id = 21
    await search.callback(value)
    value.response.send_modal.assert_not_awaited()
    value.user.id = 20
    view.stop()
    await search.callback(value)
    value.response.send_modal.assert_not_awaited()
    assert "expired" in value.followup.send.call_args.args[0]
