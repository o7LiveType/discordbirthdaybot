import calendar
import logging
from datetime import UTC, datetime
from typing import Literal

import discord
from discord import app_commands

from .delivery import celebration, channel_issues
from .timeutil import ZONES
from .timeutil import timezone as valid_timezone
from .ui import (
    BirthdayWizard,
    Confirm,
    OwnedView,
    SettingsWizard,
    button,
    defer,
    moderator,
    private,
)
from .work_queue import queued

log = logging.getLogger(__name__)


def require_mod(interaction):
    if not moderator(interaction):
        raise app_commands.MissingPermissions(["manage_guild"])
    return True


class PrivateList(OwnedView):
    def __init__(self, bot, owner, guild_id):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot, self.guild_id, self.page = bot, guild_id, 0
        self.add_item(button("Previous", self.previous))
        self.add_item(button("Next", self.next))

    async def render(self):
        # Requery on every click: don't retain deleted private records in menus.
        total = (
            await self.bot.db.read(
                "SELECT count(*) FROM birthdays WHERE guild_id=?", (self.guild_id,)
            )
        )[0][0]
        self.page = max(0, min(self.page, (total - 1) // 10))
        values = await self.bot.db.read(
            "SELECT * FROM birthdays WHERE guild_id=? ORDER BY month,day,user_id LIMIT 10 OFFSET ?",
            (self.guild_id, self.page * 10),
        )
        lines = [
            f"<@{r['user_id']}> · {r['month']:02}/{r['day']:02} · {r['timezone']} · "
            f"{r['hour']:02}:{r['minute']:02} · {'public' if r['consent'] else 'private'} · {r['leap']}"
            for r in values
        ]
        return (
            "**Birthday records · moderators only**\n"
            + ("\n".join(lines) or "No records yet.")
            + f"\nPage {self.page + 1}/{max(1, (total + 9) // 10)} · {total} records\n"
            "Dates are MM/DD. Do not share private records."
        )

    async def previous(self, interaction):
        self.page -= 1
        await defer(interaction)
        await interaction.edit_original_response(
            content=await self.render(), view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def next(self, interaction):
        self.page += 1
        await defer(interaction)
        await interaction.edit_original_response(
            content=await self.render(), view=self, allowed_mentions=discord.AllowedMentions.none()
        )


def install(bot):
    birthday = app_commands.Group(
        name="birthday",
        description="Your private birthday and celebration preferences",
        guild_only=True,
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    )
    admin = app_commands.Group(
        name="birthday-admin",
        description="Moderate private birthday records and configure celebrations",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    )

    async def open_member(interaction, timezone, opt_out, *, update):
        zone = valid_timezone(timezone) if timezone else None
        await defer(interaction)
        settings = await bot.db.guild(interaction.guild_id)
        record = await bot.db.birthday(interaction.guild_id, interaction.user.id)
        if update and not record:
            await private(interaction, "You haven't set a birthday yet. Use `/birthday set`.")
            return
        if not update and record:
            await private(
                interaction, "Your birthday is already saved. Use `/birthday update` to change it."
            )
            return
        view = BirthdayWizard(
            bot, interaction.user.id, interaction.guild_id, settings, record, zone, opt_out, update
        )
        bot.track_view(view, interaction.guild_id, interaction.user.id)
        await private(interaction, view.content(), view=view)

    @birthday.command(name="set", description="Set your birthday with a simple private menu")
    @app_commands.describe(
        opt_out="Save privately without a public birthday announcement",
        timezone="Optional: type a city to find your timezone",
    )
    @queued(bot)
    async def set_birthday(
        interaction: discord.Interaction, opt_out: bool | None = None, timezone: str | None = None
    ):
        await open_member(interaction, timezone, opt_out, update=False)

    @birthday.command(
        name="update", description="Update your birthday, timezone or announcement opt-out"
    )
    @app_commands.describe(
        opt_out="Turn birthday announcements off (True) or on (False)",
        timezone="Optional: type a city to find your timezone",
    )
    @queued(bot)
    async def update_birthday(
        interaction: discord.Interaction, opt_out: bool | None = None, timezone: str | None = None
    ):
        await open_member(interaction, timezone, opt_out, update=True)

    async def zone_autocomplete(interaction: discord.Interaction, current: str):
        needle = current.casefold().replace(" ", "_")
        return [
            app_commands.Choice(name=zone.replace("_", " "), value=zone)
            for zone in ZONES
            if needle in zone.casefold()
        ][:25]

    set_birthday.autocomplete("timezone")(zone_autocomplete)
    update_birthday.autocomplete("timezone")(zone_autocomplete)

    @birthday.command(name="me", description="See your own saved birthday and timezone privately")
    @queued(bot)
    async def me(interaction: discord.Interaction):
        await defer(interaction)
        record = await bot.db.birthday(interaction.guild_id, interaction.user.id)
        if not record:
            await private(interaction, "You have no birthday saved here. Use `/birthday set`.")
            return
        await private(
            interaction,
            f"🎂 Your birthday: **{calendar.month_name[record['month']]} {record['day']}**\n"
            f"Timezone: **{record['timezone']}**\n"
            "Use `/birthday update` to change it or `/birthday delete` to remove it.",
        )

    @birthday.command(
        name="delete", description="Delete your birthday and delivery metadata from this server"
    )
    @queued(bot)
    async def delete(interaction: discord.Interaction):
        async def action(submission):
            async with bot.mutation_lock:
                await bot.db.forget(submission.guild_id, submission.user.id)
            log.info(
                "birthday_deleted guild=%s user=%s actor=self",
                submission.guild_id,
                submission.user.id,
            )

        await private(
            interaction,
            "Delete your birthday and delivery metadata from this server? "
            "This removes your saved birthday in this server. Existing messages and operator backups are unaffected.",
            view=bot.track_view(
                Confirm(interaction.user.id, action, queue=bot.queue),
                interaction.guild_id,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    @birthday.command(name="help", description="How to save, change or remove your birthday")
    @queued(bot)
    async def help_command(interaction: discord.Interaction):
        await private(
            interaction,
            "🎂 **Set your birthday**\n"
            "`/birthday set` → choose your day, month and timezone. That's it!\n"
            "`/birthday update` → change those details.\n"
            "`/birthday me` → check what you saved.\n"
            "`/birthday delete` → remove it.\n\n"
            "Press Save to confirm. Announcements are optional: use the opt_out flag or toggle them off in the menu. Only you and Manage Server moderators "
            "can view the saved record. No birth year is collected.",
        )

    @admin.command(
        name="setup",
        description="Choose channels, announcement time, leap-day behavior and celebration GIF",
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def setup(interaction: discord.Interaction):
        await defer(interaction)
        settings = await bot.db.guild(interaction.guild_id)
        view = SettingsWizard(bot, interaction.user.id, settings)
        bot.track_view(view, interaction.guild_id, interaction.user.id)
        await interaction.followup.send(view.content(), view=view, ephemeral=True)

    @admin.command(
        name="list", description="Privately list this server's birthday records (moderators only)"
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def list_birthdays(interaction: discord.Interaction):
        await defer(interaction)
        view = PrivateList(bot, interaction.user.id, interaction.guild_id)
        bot.track_view(view, interaction.guild_id, interaction.user.id)
        await interaction.followup.send(
            await view.render(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log.info("birthdays_listed guild=%s actor=%s", interaction.guild_id, interaction.user.id)

    @admin.command(
        name="forget", description="Delete a member's record from this server after confirmation"
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def forget(interaction: discord.Interaction, member: discord.Member):
        async def action(submission):
            async with bot.mutation_lock:
                await bot.db.forget(submission.guild_id, member.id)
            log.info(
                "birthday_deleted guild=%s user=%s actor=%s",
                submission.guild_id,
                member.id,
                submission.user.id,
            )

        await private(
            interaction,
            f"Delete the stored record for <@{member.id}>? Existing messages and backups remain.",
            view=bot.track_view(
                Confirm(interaction.user.id, action, admin=True, queue=bot.queue),
                interaction.guild_id,
                interaction.user.id,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(
        name="pause", description="Pause or resume all birthday announcements for this server"
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def pause(interaction: discord.Interaction, paused: bool):
        await defer(interaction)
        async with bot.mutation_lock:
            await bot.db.ensure_guild(interaction.guild_id)
            await bot.db.pause(interaction.guild_id, paused)
        await private(
            interaction,
            "Announcements paused."
            if paused
            else "Announcements resumed. Today's due birthdays may catch up.",
        )
        log.info(
            "announcements_paused guild=%s actor=%s paused=%s",
            interaction.guild_id,
            interaction.user.id,
            paused,
        )

    @admin.command(
        name="preview",
        description="Privately preview the birthday embed and check channel permissions",
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def preview(interaction: discord.Interaction):
        await defer(interaction)
        settings = await bot.db.guild(interaction.guild_id)
        issues = channel_issues(
            interaction.guild.get_channel(settings["channel_id"] or 0), interaction.guild
        )
        await interaction.followup.send(
            "Preview only. " + (" ".join(issues) if issues else "Channel permissions OK."),
            embed=celebration(
                interaction.user.id, settings["gif"] or bot.config.gif, "", preview=True
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(
        name="status",
        description="Inspect scheduler health, configuration, SQLite durability and delivery counts",
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def status(interaction: discord.Interaction):
        await defer(interaction)
        settings = await bot.db.guild(interaction.guild_id)
        counts = await bot.db.read(
            "SELECT state,count(*) AS n FROM deliveries WHERE guild_id=? GROUP BY state",
            (interaction.guild_id,),
        )
        mode = (await bot.db.read("PRAGMA journal_mode"))[0][0]
        sync = (await bot.db.read("PRAGMA synchronous"))[0][0]
        issues = channel_issues(
            interaction.guild.get_channel(settings["channel_id"] or 0), interaction.guild
        )
        last = bot.scheduler.last_success
        age = (
            f"{(datetime.now(UTC) - last).total_seconds():.0f}s ago"
            if last
            else "not yet completed"
        )
        await private(
            interaction,
            f"**Birthday bot health**\nScheduler last success: {age}\nPoll interval: {bot.config.poll_seconds}s\n"
            f"Gateway latency: {bot.latency * 1000:.0f}ms\nSQLite: {mode.upper()}, synchronous={'FULL' if sync == 2 else sync}\n"
            f"Announcements: {'enabled' if settings['enabled'] else 'PAUSED'}\n"
            f"Channel: {settings['channel_id'] or 'NOT SET'}\nAlerts: {settings['alert_channel_id'] or 'logs only'}\n"
            f"GIF configured: {bool(settings['gif'] or bot.config.gif)}\n"
            "Deliveries: "
            + (", ".join(f"{r['state']}={r['n']}" for r in counts) or "none")
            + "\nChecks: "
            + (" ".join(issues) if issues else "channel permissions OK"),
        )

    @admin.command(
        name="deliveries", description="Inspect recent failed or uncertain birthday deliveries"
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def deliveries(interaction: discord.Interaction):
        await defer(interaction)
        rows = await bot.db.read(
            "SELECT * FROM deliveries WHERE guild_id=? AND state NOT IN ('sent','skipped') ORDER BY created_at DESC LIMIT 15",
            (interaction.guild_id,),
        )
        await private(
            interaction,
            "**Deliveries needing attention**\n"
            + (
                "\n".join(
                    f"<@{r['user_id']}> · {r['year']} · {r['state']} · marker `{r['marker']}` · {r['error'] or 'queued'}"
                    for r in rows
                )
                or "None."
            )
            + "\nResolve uncertain sends only after checking the channel for their marker.",
        )

    @admin.command(
        name="resolve",
        description="Resolve an uncertain send after manually checking the channel for duplicates",
    )
    @app_commands.check(require_mod)
    @queued(bot)
    async def resolve(
        interaction: discord.Interaction,
        member: discord.Member,
        year: app_commands.Range[int, 2020, 2200],
        action: Literal["mark_delivered", "retry", "skip"],
        checked_channel: bool,
        message_id: str | None = None,
    ):
        if not checked_channel:
            await private(
                interaction,
                "Check the delivery's original channel and marker first, then set checked_channel to True.",
            )
            return
        await defer(interaction)
        async with bot.mutation_lock:
            rows = await bot.db.read(
                "SELECT * FROM deliveries WHERE guild_id=? AND user_id=? AND year=? AND state IN ('uncertain','sending','retry')",
                (interaction.guild_id, member.id, year),
            )
            if not rows:
                await private(interaction, "No unresolved delivery matches that member and year.")
                return
            state = {"mark_delivered": "sent", "retry": "retry", "skip": "skipped"}[action]
            if state == "sent":
                if not message_id or not message_id.isdecimal():
                    await private(
                        interaction,
                        "Provide the birthday post's message_id so I can verify it and delete it after 24 hours.",
                    )
                    return
                channel = bot.get_partial_messageable(rows[0]["channel_id"])
                message = await channel.fetch_message(int(message_id))
                if message.author.id != bot.user.id or not any(
                    e.footer.text == f"Birthday delivery {rows[0]['marker']}"
                    for e in message.embeds
                ):
                    await private(
                        interaction,
                        "That message does not match this delivery's author and marker.",
                    )
                    return
                await bot.scheduler.confirm_sent(rows[0], message)
            else:
                await bot.db.delivery_state(rows[0], state, error="Manually resolved by moderator")
        log.info(
            "delivery_resolved guild=%s user=%s year=%s actor=%s action=%s",
            interaction.guild_id,
            member.id,
            year,
            interaction.user.id,
            action,
        )
        await private(
            interaction,
            f"Delivery resolved: {state}. Retries only send if it is still the user's local birthday and celebration is enabled.",
        )

    bot.tree.add_command(birthday)
    bot.tree.add_command(admin)
