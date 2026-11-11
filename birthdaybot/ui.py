import calendar
import logging
import time

import discord

from .config import gif_url
from .delivery import channel_issues
from .timeutil import anniversary, clock, timezone

log = logging.getLogger(__name__)


def moderator(interaction):
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )


async def private(interaction, content, **kwargs):
    kwargs["ephemeral"] = True
    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


async def defer(interaction):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


async def edit(interaction, **kwargs):
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.edit_message(**kwargs)


class OwnedView(discord.ui.View):
    def __init__(self, owner, *, admin=False, queue=None):
        super().__init__(timeout=600)
        self.owner = owner
        self.admin = admin
        self.last_click = 0
        self.queue = queue

    def add_item(self, item):
        if self.queue is not None and getattr(item.callback, "__name__", "") != "defaults":
            callback = item.callback

            async def run(interaction):
                # Opening a modal must be the initial response, not a deferred job.
                if isinstance(item, discord.ui.Select) and item.values == ["custom"]:
                    return await callback(interaction)
                return await self.queue.run(
                    interaction, lambda: callback(interaction), update=True, rate_checked=True
                )

            item.callback = run
        return super().add_item(item)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner or not interaction.guild:
            await private(
                interaction, "This private menu belongs to someone else. Run the command yourself."
            )
            return False
        if self.admin and not moderator(interaction):
            await private(interaction, "You need Manage Server permission to continue.")
            return False
        if time.monotonic() - self.last_click < 0.4:
            await private(interaction, "Please wait a moment between menu actions.")
            return False
        if self.queue is not None and not self.queue.admit(
            (interaction.guild_id, interaction.user.id)
        ):
            await private(interaction, "Please slow down. Menu limit: 12 actions per 10 seconds.")
            return False
        self.last_click = time.monotonic()
        return True

    async def on_error(self, interaction, error, item):
        log.error("component_failed type=%s user=%s", type(error).__name__, interaction.user.id)
        await private(
            interaction,
            "That action failed. Nothing was confirmed. Try again or ask a moderator to check the logs.",
        )


class FieldsModal(discord.ui.Modal):
    def __init__(self, title, fields, callback, owner, *, admin=False):
        super().__init__(title=title, timeout=300)
        self.submit = callback
        self.owner = owner
        self.admin = admin
        self.inputs = {}
        for key, label, default in fields:
            field = discord.ui.TextInput(
                default=default, max_length=500 if key == "gif" else 80, required=key != "gif"
            )
            self.inputs[key] = field
            self.add_item(discord.ui.Label(text=label, component=field))

    async def on_submit(self, interaction):
        if interaction.user.id != self.owner or (self.admin and not moderator(interaction)):
            await private(interaction, "You no longer have access to this form.")
            return
        try:
            await self.submit(
                interaction, {key: field.value.strip() for key, field in self.inputs.items()}
            )
        except ValueError as exc:
            await private(interaction, str(exc))

    async def on_error(self, interaction, error):
        log.error("modal_failed type=%s", type(error).__name__)
        await private(interaction, "Could not save this form. Please reopen it and try again.")


def button(label, callback, *, style=discord.ButtonStyle.secondary, row=4, disabled=False):
    item = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
    item.callback = callback
    return item


class BirthdayWizard(OwnedView):
    def __init__(
        self, bot, owner, guild_id, settings, record=None, zone=None, opt_out=None, update=False
    ):
        super().__init__(owner, queue=bot.queue)
        self.bot, self.guild_id, self.update = bot, guild_id, update
        self.month = record["month"] if record else None
        self.day = record["day"] if record else None
        self.zone = zone or (record["timezone"] if record else settings["timezone"])
        self.opt_out = (
            opt_out if opt_out is not None else (not record["consent"] if record else False)
        )
        self.day_page = int(bool(self.day and self.day >= 25))
        self.completed = False
        self.public_birthdays = bool(settings["public_birthdays"])
        self.build()

    def content(self):
        month = calendar.month_name[self.month] if self.month else "Choose below"
        return (
            "**Your birthday**\n"
            f"Month: **{month}**\nDay: **{self.day or 'Choose below'}**\n"
            f"Timezone: **{self.zone.replace('_', ' ')}**\n"
            f"Birthday sharing: **{'OFF (opted out)' if self.opt_out else 'ON'}**\n\n"
            "Choose your details, then **Save**. "
            + (
                "Your month and day are shared publicly when announcements are on. "
                if self.public_birthdays
                else "Birthday browsing is restricted to moderators in this server. "
            )
            + "Turn announcements OFF to hide your date from public lists and birthday posts."
        )

    def build(self):
        self.clear_items()
        months = discord.ui.Select(
            placeholder="Month",
            row=0,
            options=[
                discord.SelectOption(
                    label=calendar.month_name[m], value=str(m), default=m == self.month
                )
                for m in range(1, 13)
            ],
        )

        async def choose_month(interaction):
            self.month = int(months.values[0])
            if self.day and self.day > calendar.monthrange(2000, self.month)[1]:
                self.day = None
            self.build()
            await edit(interaction, content=self.content(), view=self)

        months.callback = choose_month
        self.add_item(months)
        maximum = calendar.monthrange(2000, self.month)[1] if self.month else 31
        start, end = (25, maximum) if self.day_page else (1, 24)
        days = discord.ui.Select(
            placeholder="Day" + (" (25–31)" if self.day_page else " (1–24)"),
            row=1,
            options=[
                discord.SelectOption(label=str(d), value=str(d), default=d == self.day)
                for d in range(start, end + 1)
            ]
            + [
                discord.SelectOption(
                    label="← Days 1–24" if self.day_page else "More days: 25–31 →", value="page"
                )
            ],
        )

        async def choose_day(interaction):
            if days.values[0] == "page":
                self.day_page = 1 - self.day_page
            else:
                self.day = int(days.values[0])
            self.build()
            await edit(interaction, content=self.content(), view=self)

        days.callback = choose_day
        self.add_item(days)
        common = [
            "UTC",
            "America/Denver",
            "America/New_York",
            "America/Chicago",
            "America/Los_Angeles",
            "America/Phoenix",
            "America/Anchorage",
            "Pacific/Honolulu",
            "America/Toronto",
            "America/Vancouver",
            "America/Sao_Paulo",
            "Europe/London",
            "Europe/Paris",
            "Europe/Berlin",
            "Europe/Helsinki",
            "Asia/Dubai",
            "Asia/Kolkata",
            "Asia/Kathmandu",
            "Asia/Singapore",
            "Asia/Tokyo",
            "Australia/Sydney",
            "Australia/Perth",
            "Pacific/Auckland",
        ]
        zones = list(dict.fromkeys([self.zone] + common))[:24]
        select = discord.ui.Select(
            placeholder="Timezone",
            row=2,
            options=[
                discord.SelectOption(label=z.replace("_", " "), value=z, default=z == self.zone)
                for z in zones
            ]
            + [discord.SelectOption(label="Other timezone…", value="custom")],
        )

        async def choose_zone(interaction):
            if select.values[0] == "custom":

                async def submit(submission, values):
                    value = timezone(values["zone"])
                    if self.is_finished():
                        await private(submission, "This menu expired. Run the command again.")
                        return
                    self.zone = value
                    self.build()
                    await edit(submission, content=self.content(), view=self)

                await interaction.response.send_modal(
                    FieldsModal(
                        "Your timezone",
                        [("zone", "IANA timezone, e.g. America/Denver", self.zone)],
                        submit,
                        self.owner,
                    )
                )
                return
            self.zone = select.values[0]
            self.build()
            await edit(interaction, content=self.content(), view=self)

        select.callback = choose_zone
        self.add_item(select)
        self.add_item(
            button(
                "Save",
                self.save,
                style=discord.ButtonStyle.success,
                row=3,
                disabled=not (self.month and self.day),
            )
        )
        self.add_item(button("Sharing: " + ("OFF" if self.opt_out else "ON"), self.toggle, row=3))
        self.add_item(button("Cancel", self.cancel, row=3))

    async def toggle(self, interaction):
        self.opt_out = not self.opt_out
        self.build()
        await edit(interaction, content=self.content(), view=self)

    async def save(self, interaction):
        if self.completed:
            await private(interaction, "This menu was already saved.")
            return
        anniversary(self.month, self.day, 2000, "feb28")
        await defer(interaction)
        async with self.bot.mutation_lock:
            settings = await self.bot.db.guild(self.guild_id)
            record = await self.bot.db.birthday(self.guild_id, self.owner)
            if self.update and not record:
                await private(
                    interaction, "Your record was deleted. Use /birthday set to create it again."
                )
                return
            if not self.update and record:
                await private(
                    interaction,
                    "Your birthday was already saved. Use /birthday update to change it.",
                )
                return
            await self.bot.db.save(
                self.guild_id,
                self.owner,
                self.month,
                self.day,
                self.zone,
                settings["hour"],
                settings["minute"],
                settings["leap"],
                not self.opt_out,
            )
        self.completed = True
        self.stop()
        result = (
            f"✅ Birthday {'updated' if self.update else 'saved'}: **{calendar.month_name[self.month]} {self.day}**\n"
            f"Timezone: **{self.zone.replace('_', ' ')}**\n"
            + (
                "You opted out of public birthday lists and announcements."
                if self.opt_out
                else "We'll celebrate in the birthday channel on your birthday."
                + (
                    " Your month and day are also shared in the public birthday list."
                    if settings["public_birthdays"]
                    else ""
                )
            )
            + "\nUse `/birthday update` to change it or `/birthday delete` to remove it."
        )
        if not settings["channel_id"] or not settings["enabled"]:
            result += "\n⚠️ A moderator needs to enable the birthday channel before announcements can post."
        await interaction.edit_original_response(content=result, view=None)
        log.info(
            "birthday_saved guild=%s user=%s action=%s opted_out=%s",
            self.guild_id,
            self.owner,
            "update" if self.update else "set",
            self.opt_out,
        )

    async def cancel(self, interaction):
        self.stop()
        await edit(interaction, content="Cancelled. Nothing was saved.", view=None)


class Confirm(OwnedView):
    def __init__(self, owner, callback, *, admin=False, queue=None):
        super().__init__(owner, admin=admin, queue=queue)
        self.action = callback
        self.completed = False
        self.add_item(button("Confirm deletion", self.confirm, style=discord.ButtonStyle.danger))
        self.add_item(button("Cancel", self.cancel))

    async def confirm(self, interaction):
        if self.completed:
            await private(interaction, "Already completed.")
            return
        await defer(interaction)
        await self.action(interaction)
        self.completed = True
        self.stop()
        await interaction.edit_original_response(
            content="✅ Birthday record deleted from this server.", view=None
        )

    async def cancel(self, interaction):
        self.stop()
        await edit(interaction, content="Deletion cancelled.", view=None)


class SettingsWizard(OwnedView):
    def __init__(self, bot, owner, settings):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot = bot
        self.guild_id = settings["guild_id"]
        self.channel_id, self.alert_id = settings["channel_id"], settings["alert_channel_id"]
        self.zone = settings["timezone"]
        self.hour, self.minute = settings["hour"], settings["minute"]
        self.gif = settings["gif"]
        self.leap = settings["leap"]
        self.public_birthdays = bool(settings["public_birthdays"])
        for row, label, attr in [
            (0, "Birthday announcements channel", "channel_id"),
            (1, "Optional moderator alerts channel", "alert_id"),
        ]:
            select = discord.ui.ChannelSelect(
                placeholder=label, channel_types=[discord.ChannelType.text], row=row
            )

            def callback_for(component, attribute):
                async def changed(interaction):
                    setattr(self, attribute, component.values[0].id)
                    await edit(interaction, content=self.content(), view=self)

                return changed

            select.callback = callback_for(select, attr)
            self.add_item(select)
        leap = discord.ui.Select(
            placeholder="February 29 birthdays",
            row=2,
            options=[
                discord.SelectOption(label=label, value=value, default=value == self.leap)
                for label, value in [
                    ("Non-leap years: celebrate February 28", "feb28"),
                    ("Non-leap years: celebrate March 1", "mar1"),
                    ("Only celebrate February 29 in leap years", "leap_only"),
                ]
            ],
        )

        async def choose_leap(interaction):
            self.leap = leap.values[0]
            await edit(interaction, content=self.content(), view=self)

        leap.callback = choose_leap
        self.add_item(leap)
        visibility = discord.ui.Select(
            placeholder="Birthday list access",
            row=3,
            options=[
                discord.SelectOption(
                    label="Public — everyone can browse",
                    value="public",
                    default=self.public_birthdays,
                ),
                discord.SelectOption(
                    label="Private — moderators only",
                    value="private",
                    default=not self.public_birthdays,
                ),
            ],
        )

        async def choose_visibility(interaction):
            self.public_birthdays = visibility.values[0] == "public"
            await edit(interaction, content=self.content(), view=self)

        visibility.callback = choose_visibility
        self.add_item(visibility)
        self.add_item(button("Time / GIF", self.defaults))
        self.add_item(button("Clear alerts", self.clear_alerts))
        self.add_item(button("Save settings", self.save, style=discord.ButtonStyle.success))

    def content(self):
        return (
            f"**Birthday bot setup · moderators only**\n"
            f"Birthday channel: {f'<#{self.channel_id}>' if self.channel_id else 'NOT SET'}\n"
            f"Alerts channel: {f'<#{self.alert_id}>' if self.alert_id else 'OFF — warnings in logs only'}\n"
            f"Birthday list access: **{'Public' if self.public_birthdays else 'Moderators only'}** · opted-out dates stay hidden publicly\n"
            f"Announcement time: **{self.hour:02}:{self.minute:02} in each member's timezone**\n"
            f"February 29 rule: **{ {'feb28': 'February 28', 'mar1': 'March 1', 'leap_only': 'leap years only'}[self.leap] }**\n"
            "Time and leap-day changes apply to all birthdays in this server.\n"
            "Alerts repeat at most hourly when setup or delivery needs attention. "
            "Choose a private moderator channel for alerts. Save applies all settings."
        )

    async def defaults(self, interaction):
        async def submit(submission, values):
            hour, minute = clock(values["time"])
            gif = gif_url(values["gif"]) if values["gif"] else ""
            if self.is_finished():
                await private(submission, "This menu expired. Run /birthday-admin setup again.")
                return
            self.hour, self.minute, self.gif = hour, minute, gif
            await edit(submission, content=self.content(), view=self)

        await interaction.response.send_modal(
            FieldsModal(
                "Announcement time and birthday GIF",
                [
                    (
                        "time",
                        "Time in each member's timezone (HH:MM)",
                        f"{self.hour:02}:{self.minute:02}",
                    ),
                    ("gif", "Direct KLIPY GIF URL (blank uses .env default)", self.gif),
                ],
                submit,
                self.owner,
                admin=True,
            )
        )

    async def clear_alerts(self, interaction):
        self.alert_id = None
        await edit(interaction, content=self.content(), view=self)

    async def save(self, interaction):
        guild = interaction.guild
        issues = (
            channel_issues(guild.get_channel(self.channel_id), guild) if self.channel_id else []
        )
        if self.alert_id:
            issues += channel_issues(guild.get_channel(self.alert_id), guild)
        if not (self.gif or self.bot.config.gif):
            issues.append("Choose a celebration GIF in Time / GIF first.")
        if issues:
            await private(interaction, "Cannot save yet:\n" + "\n".join(issues))
            return
        await defer(interaction)
        async with self.bot.mutation_lock:
            await self.bot.db.configure(
                self.guild_id,
                self.channel_id,
                self.alert_id,
                self.zone,
                self.hour,
                self.minute,
                self.gif,
                self.leap,
                self.public_birthdays,
            )
        self.stop()
        await interaction.edit_original_response(
            content="✅ Settings saved.\n" + self.content(), view=None
        )
        log.info(
            "guild_configured guild=%s actor=%s channel=%s",
            self.guild_id,
            self.owner,
            self.channel_id,
        )
