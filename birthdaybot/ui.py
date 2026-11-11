import calendar
import logging
import time

import discord

from .config import DEFAULT_GIF, gif_url
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
        if self.queue is not None:
            callback = item.callback

            async def run(interaction):
                # Opening a modal must be the initial response, not a deferred job.
                if (
                    isinstance(item, discord.ui.Select)
                    and item.values
                    and item.values[0] in getattr(callback, "opens_modal_values", {"custom"})
                ):
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
                default=default, max_length=2048 if key == "gif" else 80, required=key != "gif"
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
    SECTIONS = [
        ("Overview", "overview"),
        ("Birthday channel", "channel"),
        ("Moderator alerts", "alerts"),
        ("Announcement time", "time"),
        ("Birthday GIF link", "gif"),
        ("Birthday list access", "privacy"),
        ("February 29 birthdays", "leap"),
    ]

    def __init__(self, bot, owner, settings):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot, self.guild_id = bot, settings["guild_id"]
        self.channel_id, self.alert_id = settings["channel_id"], settings["alert_channel_id"]
        self.zone = settings["timezone"]
        self.hour, self.minute = settings["hour"], settings["minute"]
        try:
            self.gif = gif_url(settings["gif"]) if settings["gif"] else ""
        except ValueError:
            self.gif = ""
            log.warning("invalid_saved_gif_replaced guild=%s", self.guild_id)
        self.leap = settings["leap"]
        self.public_birthdays = bool(settings["public_birthdays"])
        self.section = "overview"
        self.build()

    def build(self):
        self.clear_items()
        menu = discord.ui.Select(
            placeholder="Choose a setting",
            row=0,
            options=[
                discord.SelectOption(label=label, value=value, default=value == self.section)
                for label, value in self.SECTIONS
            ],
        )

        async def navigate(interaction):
            choice = menu.values[0]
            if choice == "time":
                await self.edit_time(interaction)
                return
            if choice == "gif":
                await self.edit_gif(interaction)
                return
            self.section = choice
            self.build()
            await edit(interaction, content=self.content(), view=self)

        navigate.opens_modal_values = {"time", "gif"}
        menu.callback = navigate
        self.add_item(menu)
        if self.section in {"channel", "alerts"}:
            channel = discord.ui.ChannelSelect(
                placeholder="Choose a birthday channel"
                if self.section == "channel"
                else "Choose a moderator alerts channel",
                channel_types=[discord.ChannelType.text],
                row=1,
            )
            attribute = "channel_id" if self.section == "channel" else "alert_id"

            async def choose_channel(interaction):
                setattr(self, attribute, channel.values[0].id)
                await edit(interaction, content=self.content(), view=self)

            channel.callback = choose_channel
            self.add_item(channel)
            if self.section == "alerts":
                self.add_item(button("Turn alerts off", self.clear_alerts, row=2))
        elif self.section in {"privacy", "leap"}:
            options = (
                [
                    ("Public — everyone can browse", "public"),
                    ("Private — moderators only", "private"),
                ]
                if self.section == "privacy"
                else [
                    ("Celebrate February 28 in non-leap years", "feb28"),
                    ("Celebrate March 1 in non-leap years", "mar1"),
                    ("Celebrate only in leap years", "leap_only"),
                ]
            )
            selected = (
                ("public" if self.public_birthdays else "private")
                if self.section == "privacy"
                else self.leap
            )
            choice = discord.ui.Select(
                placeholder="Choose birthday list access"
                if self.section == "privacy"
                else "Choose the February 29 rule",
                row=1,
                options=[
                    discord.SelectOption(label=label, value=value, default=value == selected)
                    for label, value in options
                ],
            )
            section = self.section

            async def choose_option(interaction):
                if section == "privacy":
                    self.public_birthdays = choice.values[0] == "public"
                else:
                    self.leap = choice.values[0]
                self.build()
                await edit(interaction, content=self.content(), view=self)

            choice.callback = choose_option
            self.add_item(choice)
        elif self.section == "time":
            self.add_item(button("Midnight (00:00)", self.reset_midnight, row=2))
        self.add_item(button("Save settings", self.save, style=discord.ButtonStyle.success))
        self.add_item(button("Cancel", self.cancel))

    def content(self):
        default = getattr(self.bot.config, "gif", DEFAULT_GIF)
        default = gif_url(default) if isinstance(default, str) and default else DEFAULT_GIF
        return (
            "**Birthday bot settings**\n"
            f"Birthday channel: {f'<#{self.channel_id}>' if self.channel_id else 'Not set'}\n"
            f"Moderator alerts: {f'<#{self.alert_id}>' if self.alert_id else 'Off'}\n"
            f"Announcement time: **{self.hour:02}:{self.minute:02} in each member's timezone**\n"
            f"Birthday GIF: [Open GIF]({self.gif or default})\n"
            f"Birthday lists: **{'Public' if self.public_birthdays else 'Moderators only'}** · opted-out dates stay hidden publicly\n"
            f"February 29: **{ {'feb28': 'February 28', 'mar1': 'March 1', 'leap_only': 'leap years only'}[self.leap] }**\n\n"
            "Choose a setting from the menu, then **Save settings**. **00:00 is local midnight.** "
            "The time and leap-day rule apply to every birthday in this server."
        )

    async def edit_time(self, interaction):
        async def submit(submission, values):
            hour, minute = clock(values["time"])
            if self.is_finished():
                await private(submission, "This menu expired. Run /birthday-admin setup again.")
                return
            self.hour, self.minute, self.section = hour, minute, "time"
            self.build()
            await edit(submission, content=self.content(), view=self)

        await interaction.response.send_modal(
            FieldsModal(
                "Announcement time",
                [
                    (
                        "time",
                        "Local time (HH:MM; 00:00 = midnight)",
                        f"{self.hour:02}:{self.minute:02}",
                    )
                ],
                submit,
                self.owner,
                admin=True,
            )
        )

    async def edit_gif(self, interaction):
        async def submit(submission, values):
            value = gif_url(values["gif"]) if values["gif"] else ""
            if self.is_finished():
                await private(submission, "This menu expired. Run /birthday-admin setup again.")
                return
            self.gif, self.section = value, "gif"
            self.build()
            await edit(submission, content=self.content(), view=self)

        await interaction.response.send_modal(
            FieldsModal(
                "Birthday GIF link",
                [("gif", "HTTPS .gif URL (blank uses default)", self.gif)],
                submit,
                self.owner,
                admin=True,
            )
        )

    async def reset_midnight(self, interaction):
        self.hour, self.minute, self.section = 0, 0, "time"
        self.build()
        await edit(interaction, content=self.content(), view=self)

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
            issues.append("Choose a birthday GIF link first.")
        if issues:
            await private(interaction, "Cannot save yet:\n" + "\n".join(issues))
            return
        self.gif = gif_url(self.gif) if self.gif else ""
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

    async def cancel(self, interaction):
        self.stop()
        await edit(interaction, content="Cancelled. Settings were not changed.", view=None)
