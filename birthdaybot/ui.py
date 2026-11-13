import calendar
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord

from .config import DEFAULT_GIF, gif_url
from .delivery import channel_issues
from .embeds import birthday_embed, manager_embed, text_embed
from .sharing import FLAGS, LABELS, Sharing
from .timeutil import anniversary, format_offset, timezone, timezone_label
from .timezone_catalog import REGIONS, catalog, search_timezones

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
    content = kwargs.get("content")
    view = kwargs.get("view")
    if (
        isinstance(content, str)
        and kwargs.get("embed") is None
        and isinstance(view, OwnedView)
        and not getattr(view, "public", False)
    ):
        renderer = getattr(view, "make_embed", None)
        kwargs["content"] = None
        kwargs["embed"] = renderer(interaction) if renderer else text_embed(content)
    elif isinstance(content, str) and len(content) > 2000:
        # A valid media URL can be 2048 characters on its own. Keep the full
        # clickable link in an embed rather than sending an invalid message.
        kwargs["content"] = None
        kwargs["embed"] = discord.Embed(description=content, color=0xF6B5D7)
    elif isinstance(content, str):
        kwargs.setdefault("embed", None)
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
                if getattr(callback, "opens_modal", False) or (
                    isinstance(item, discord.ui.Select)
                    and item.values
                    and item.values[0] in getattr(callback, "opens_modal_values", {"custom"})
                ):
                    if await self.check_access(interaction):
                        return await callback(interaction)
                    return

                async def execute():
                    if await self.check_access(interaction):
                        return await callback(interaction)

                return await self.queue.run(interaction, execute, update=True, rate_checked=True)

            item.callback = run
        return super().add_item(item)

    async def check_access(self, interaction):
        if self.is_finished():
            await private(interaction, "Menu expired. Open /birthdaybot.")
            return False
        if (
            (not getattr(self, "public", False) and interaction.user.id != self.owner)
            or not interaction.guild
            or interaction.guild_id != getattr(self, "guild_id", interaction.guild_id)
            or (getattr(self, "public", False) and not isinstance(interaction.user, discord.Member))
        ):
            await private(
                interaction,
                "This menu belongs to another member.",
            )
            return False
        if self.admin and not moderator(interaction):
            await private(interaction, "Manage Server required.")
            return False
        return True

    async def interaction_check(self, interaction):
        if not await self.check_access(interaction):
            return False
        if time.monotonic() - self.last_click < 0.4:
            await private(interaction, "Wait a moment.")
            return False
        if self.queue is not None and not self.queue.admit(
            (interaction.guild_id, interaction.user.id)
        ):
            await private(interaction, "Limit: 12 actions per 10 seconds.")
            return False
        self.last_click = time.monotonic()
        return True

    async def on_error(self, interaction, error, item):
        if isinstance(error, ValueError):
            await private(interaction, str(error))
            return
        log.error("component_failed type=%s user=%s", type(error).__name__, interaction.user.id)
        await private(
            interaction,
            "Action failed. Try again.",
        )


class FieldsModal(discord.ui.Modal):
    def __init__(self, title, fields, callback, owner, *, admin=False, queue=None, parent=None):
        super().__init__(title=title, timeout=300)
        self.submit = callback
        self.owner = owner
        self.admin = admin
        self.queue = queue
        self.parent = parent
        self.inputs = {}
        for key, label, default in fields:
            field = discord.ui.TextInput(
                default=default, max_length=2048 if key == "gif" else 80, required=key != "gif"
            )
            self.inputs[key] = field
            self.add_item(discord.ui.Label(text=label, component=field))

    def authorized(self, interaction):
        return (
            bool(interaction.guild)
            and interaction.user.id == self.owner
            and (not self.admin or moderator(interaction))
            and (
                self.parent is None
                or (not self.parent.is_finished() and interaction.guild_id == self.parent.guild_id)
            )
        )

    async def on_submit(self, interaction):
        if not self.authorized(interaction):
            await private(interaction, "Form expired or access revoked.")
            return
        try:

            async def submit():
                if not self.authorized(interaction):
                    await private(
                        interaction,
                        "Form expired or access revoked. Open /birthdaybot.",
                    )
                    return
                await self.submit(
                    interaction, {key: field.value for key, field in self.inputs.items()}
                )

            if self.queue is None:
                await submit()
            else:
                await self.queue.run(interaction, submit, update=True)
        except ValueError as exc:
            await private(interaction, str(exc))

    async def on_error(self, interaction, error):
        log.error("modal_failed type=%s", type(error).__name__)
        await private(interaction, "Form failed. Reopen it to retry.")


def button(
    label,
    callback,
    *,
    style=discord.ButtonStyle.secondary,
    row=4,
    disabled=False,
    opens_modal=False,
):
    item = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
    if opens_modal:

        async def open_form(interaction):
            await callback(interaction)

        open_form.opens_modal = True
        item.callback = open_form
    else:
        item.callback = callback
    return item


def navigation(bot, owner, guild_id, *, admin=False):
    if not hasattr(bot, "menu_actions"):
        return None
    view = OwnedView(owner, admin=admin, queue=bot.queue)
    view.add_item(button("Main menu", bot.menu_actions["home"]))
    if admin:
        view.add_item(button("Server tools", bot.menu_actions["tools"]))
    return bot.track_view(view, guild_id, owner)


class SharingEditor:
    def __init__(self, view, flags, save, cancel, *, save_label="Save"):
        self.view, self.flags, self.save, self.cancel = view, flags, save, cancel
        self.save_label = save_label

    def build(self):
        self.view.clear_items()
        for index, (key, label) in enumerate(zip(FLAGS, LABELS, strict=True)):

            def callback_for(permission):
                async def toggle(interaction):
                    self.flags = replace(
                        self.flags, **{permission: not getattr(self.flags, permission)}
                    )
                    self.build()
                    await edit(interaction, content="**Sharing**", view=self.view)

                return toggle

            enabled = getattr(self.flags, key)
            self.view.add_item(
                button(
                    f"{label}: {'On' if enabled else 'Off'}",
                    callback_for(key),
                    row=index // 2,
                    style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                )
            )
        self.view.add_item(
            button(self.save_label, self.save, row=2, style=discord.ButtonStyle.primary)
        )
        self.view.add_item(button("Cancel", self.cancel, row=2))


class SharingView(OwnedView):
    def __init__(self, bot, owner, guild_id, record):
        super().__init__(owner, queue=bot.queue)
        self.bot, self.guild_id = bot, guild_id
        self.pending = record["timezone"] is None
        self.editor = SharingEditor(self, Sharing.from_record(record), self.save, self.cancel)
        self.editor.build()

    def content(self):
        return "**Sharing**"

    def make_embed(self, interaction):
        return manager_embed("Sharing", "Pending timezone" if self.pending else None)

    async def save(self, interaction):
        if not await self.check_access(interaction):
            return
        async with self.bot.mutation_lock:
            await self.bot.db.set_sharing(self.guild_id, self.owner, self.editor.flags)
        log.info(
            "sharing_saved guild=%s user=%s list=%s posts=%s dms=%s mentions=%s",
            self.guild_id,
            self.owner,
            *self.editor.flags.values(),
        )
        embed = manager_embed("Sharing saved")
        for label, enabled in zip(LABELS, self.editor.flags.values(), strict=True):
            embed.add_field(name=label, value="On" if enabled else "Off", inline=True)
        self.stop()
        await edit(
            interaction,
            content=None,
            embed=embed,
            view=navigation(self.bot, self.owner, self.guild_id),
        )

    async def cancel(self, interaction):
        self.stop()
        await edit(
            interaction, content="Cancelled.", view=navigation(self.bot, self.owner, self.guild_id)
        )


class TimezoneBrowser:
    """A screen inside the birthday draft, sharing its owner, queue and expiry."""

    PAGE_SIZE = 25
    OFFSET_PAGE_SIZE = 22

    def __init__(self, parent):
        self.parent = parent
        self.region = self.offset = self.query = self.selected = None
        self.page = self.offset_page = 0
        self.invalid = False
        self.notice = ""

    def results(self):
        entries = catalog()
        if self.invalid:
            return []
        if self.query:
            entries = search_timezones(self.query, entries)
        return [
            entry
            for entry in entries
            if (self.region is None or entry.region == self.region)
            and (self.offset is None or entry.offset == self.offset)
        ]

    def content(self):
        matches = self.results()
        pages = max(1, (len(matches) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        selected = next((entry for entry in matches if entry.name == self.selected), None)
        lines = [
            "**Timezone**",
            f"Region: {REGIONS.get(self.region, 'All')} · Offset: {format_offset(self.offset) if self.offset is not None else 'Any'}",
            f"{len(matches)} matches · Page {self.page + 1}/{pages}",
        ]
        if self.notice:
            lines.append(self.notice)
        if selected:
            local = datetime.now(UTC).astimezone(ZoneInfo(selected.name))
            lines += [timezone_label(selected.name), f"Local time: {local:%a %d %b, %H:%M}"]
        return "\n".join(lines)

    async def refresh(self, interaction):
        self.parent.build()
        await edit(interaction, content=self.parent.content(), view=self.parent)

    def build(self):
        parent = self.parent
        regions = discord.ui.Select(
            placeholder="Region",
            row=0,
            options=[
                discord.SelectOption(label="All regions", value="all", default=self.region is None),
                *[
                    discord.SelectOption(label=label, value=key, default=self.region == key)
                    for key, label in REGIONS.items()
                ],
            ],
        )

        async def choose_region(interaction):
            self.region = None if regions.values[0] == "all" else regions.values[0]
            self.page, self.query, self.selected, self.invalid, self.notice = (
                0,
                None,
                None,
                False,
                "",
            )
            await self.refresh(interaction)

        regions.callback = choose_region
        parent.add_item(regions)
        values = sorted({entry.offset for entry in catalog()})
        pages = max(1, (len(values) + self.OFFSET_PAGE_SIZE - 1) // self.OFFSET_PAGE_SIZE)
        self.offset_page = max(0, min(self.offset_page, pages - 1))
        options = [
            discord.SelectOption(
                label="Any current UTC offset", value="all", default=self.offset is None
            )
        ]
        options += [
            discord.SelectOption(
                label=format_offset(value), value=str(value), default=self.offset == value
            )
            for value in values[
                self.offset_page * self.OFFSET_PAGE_SIZE : (self.offset_page + 1)
                * self.OFFSET_PAGE_SIZE
            ]
        ]
        if self.offset_page:
            options.append(discord.SelectOption(label="← Previous UTC offsets", value="previous"))
        if self.offset_page + 1 < pages:
            options.append(discord.SelectOption(label="More UTC offsets →", value="next"))
        offsets = discord.ui.Select(placeholder="UTC offset", row=1, options=options)

        async def choose_offset(interaction):
            value = offsets.values[0]
            if value in {"previous", "next"}:
                self.offset_page += 1 if value == "next" else -1
            else:
                self.offset = None if value == "all" else int(value)
                self.page, self.query, self.selected, self.invalid, self.notice = (
                    0,
                    None,
                    None,
                    False,
                    "",
                )
            await self.refresh(interaction)

        offsets.callback = choose_offset
        parent.add_item(offsets)
        matches = self.results()
        self.page = max(0, min(self.page, max(0, (len(matches) - 1) // self.PAGE_SIZE)))
        if matches:
            cities = discord.ui.Select(
                placeholder="City / timezone",
                row=2,
                options=[
                    discord.SelectOption(
                        label=entry.label,
                        description=entry.description,
                        value=entry.name,
                        default=entry.name == self.selected,
                    )
                    for entry in matches[
                        self.page * self.PAGE_SIZE : (self.page + 1) * self.PAGE_SIZE
                    ]
                ],
            )

            async def choose_city(interaction):
                if cities.values[0] not in {entry.name for entry in self.results()}:
                    raise ValueError("Choose a timezone from the current results.")
                self.selected = timezone(cities.values[0])
                await self.refresh(interaction)

            cities.callback = choose_city
            parent.add_item(cities)
        parent.add_item(button("Previous cities", self.previous, row=3, disabled=self.page == 0))
        parent.add_item(
            button(
                "Next cities",
                self.next,
                row=3,
                disabled=(self.page + 1) * self.PAGE_SIZE >= len(matches),
            )
        )
        parent.add_item(button("Search", parent.open_zone_search, row=3, opens_modal=True))
        parent.add_item(button("Reset filters", self.reset, row=3))
        parent.add_item(
            button(
                "Use this timezone",
                self.confirm,
                row=4,
                style=discord.ButtonStyle.success,
                disabled=self.selected not in {entry.name for entry in matches},
            )
        )
        parent.add_item(button("Back to birthday", self.back, row=4))

    async def previous(self, interaction):
        self.page -= 1
        await self.refresh(interaction)

    async def next(self, interaction):
        self.page += 1
        await self.refresh(interaction)

    async def reset(self, interaction):
        self.region = self.offset = self.query = self.selected = None
        self.page = self.offset_page = 0
        self.invalid, self.notice = False, ""
        await self.refresh(interaction)

    async def search(self, interaction, query):
        # A search starts fresh; previous region/offset filters cannot hide matches.
        self.region = self.offset = self.selected = None
        self.page = 0
        try:
            matches = search_timezones(query)
        except ValueError as exc:
            self.query, self.invalid = None, True
            self.notice = str(exc)
        else:
            self.query, self.invalid = query, False
            self.selected = matches[0].name if len(matches) == 1 else None
            self.notice = ""
        await self.refresh(interaction)

    async def confirm(self, interaction):
        if self.selected not in {entry.name for entry in self.results()}:
            raise ValueError("Choose a valid timezone from the current results.")
        self.parent.zone = timezone(self.selected)
        self.parent.zone_valid = True
        self.parent.zone_notice = ""
        await self.back(interaction)

    async def back(self, interaction):
        self.parent.browsing_zone = False
        await self.refresh(interaction)


class BirthdayWizard(OwnedView):
    def __init__(
        self,
        bot,
        owner,
        guild_id,
        settings,
        record=None,
        zone=None,
        opt_out=None,
        update=False,
        timezone_verified=True,
    ):
        super().__init__(owner, queue=bot.queue)
        self.bot, self.guild_id, self.update = bot, guild_id, update
        self.month = record["month"] if record else None
        self.day = record["day"] if record else None
        self.zone = zone or (record["timezone"] if record else None) or settings["timezone"]
        self.sharing = Sharing.from_record(record) if record is not None else Sharing.all(True)
        if opt_out is not None:
            self.opt_out = opt_out
        self.editing_sharing = False
        self.day_page = int(bool(self.day and self.day >= 25))
        self.completed = False
        self.public_birthdays = bool(settings["public_birthdays"])
        self.hour, self.minute, self.leap = settings["hour"], settings["minute"], settings["leap"]
        self.zone_valid = bool(timezone_verified)
        self.browsing_zone = False
        self.zone_browser = TimezoneBrowser(self)
        self.zone_notice = ""
        self.build()

    @property
    def opt_out(self):
        return not self.sharing.enabled

    @opt_out.setter
    def opt_out(self, value):
        self.sharing = Sharing.all(not value)

    def content(self):
        if self.editing_sharing:
            return "**Sharing**"
        if self.browsing_zone:
            return self.zone_browser.content()
        month = calendar.month_name[self.month] if self.month else "—"
        lines = [
            "**Your birthday**",
            f"Month: {month} · Day: {self.day or '—'}",
            f"Timezone: {timezone_label(self.zone) if self.zone_valid else 'Required'}",
            f"Sharing: {'Off' if self.opt_out else 'On'}",
        ]
        if self.zone_notice:
            lines.append(self.zone_notice)
        return "\n".join(lines)

    def make_embed(self, interaction):
        if self.editing_sharing:
            return manager_embed("Sharing")
        if self.browsing_zone:
            return text_embed(self.zone_browser.content())
        record = dict(
            month=self.month,
            day=self.day,
            timezone=self.zone,
            hour=self.hour,
            minute=self.minute,
            leap=self.leap,
            consent=self.sharing.enabled,
            **self.sharing.record_values(),
        )
        return birthday_embed(
            interaction.user,
            record,
            draft=True,
            zone_valid=self.zone_valid,
            public_birthdays=self.public_birthdays,
            notice=self.zone_notice,
        )

    def build(self):
        self.clear_items()
        if self.editing_sharing:
            self.sharing_editor.build()
            return
        if self.browsing_zone:
            self.zone_browser.build()
            return
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
            placeholder="Timezone · quick choices",
            row=2,
            options=[
                discord.SelectOption(
                    label=timezone_label(z), value=z, default=z == self.zone and self.zone_valid
                )
                for z in zones
            ]
            + [discord.SelectOption(label="Search city / region / UTC offset…", value="custom")],
        )

        async def choose_zone(interaction):
            if select.values[0] == "custom":
                await self.open_zone_search(interaction)
                return
            self.zone = timezone(select.values[0])
            self.zone_valid = True
            self.zone_notice = ""
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
                disabled=not (self.month and self.day and self.zone_valid),
            )
        )
        self.add_item(button("Sharing", self.open_sharing, row=3))
        self.add_item(button("Cancel", self.cancel, row=3))

        self.add_item(button("Browse all timezones", self.browse_zones))
        self.add_item(button("Search timezones", self.open_zone_search, opens_modal=True))

    async def browse_zones(self, interaction):
        self.zone_browser = TimezoneBrowser(self)
        self.browsing_zone = True
        self.build()
        await edit(interaction, content=self.content(), view=self)

    async def open_zone_search(self, interaction):
        async def submit(submission, values):
            await self.search_zone(submission, values["zone"])

        await interaction.response.send_modal(
            FieldsModal(
                "Search timezones",
                [("zone", "City, region or UTC offset (e.g. UTC+05:45)", "")],
                submit,
                self.owner,
                queue=self.queue,
                parent=self,
            )
        )

    async def search_zone(self, interaction, query):
        self.browsing_zone = True
        await self.zone_browser.search(interaction, query)

    async def open_sharing(self, interaction):
        self.editing_sharing = True
        self.sharing_editor = SharingEditor(
            self, self.sharing, self.apply_sharing, self.cancel_sharing, save_label="Apply"
        )
        self.build()
        await edit(interaction, content=self.content(), view=self)

    async def apply_sharing(self, interaction):
        self.sharing = self.sharing_editor.flags
        await self.cancel_sharing(interaction)

    async def cancel_sharing(self, interaction):
        self.editing_sharing = False
        self.build()
        await edit(interaction, content=self.content(), view=self)

    async def save(self, interaction):
        if self.completed:
            await private(interaction, "This menu was already saved.")
            return
        if not self.zone_valid:
            raise ValueError("Choose a valid timezone from the menu before saving.")
        self.zone = timezone(self.zone)
        anniversary(self.month, self.day, 2000, "feb28")
        await defer(interaction)
        async with self.bot.mutation_lock:
            settings = await self.bot.db.guild(self.guild_id)
            record = await self.bot.db.birthday(self.guild_id, self.owner)
            if self.update and not record:
                await private(interaction, "Birthday deleted. Use Set birthday.")
                return
            if not self.update and record:
                await private(
                    interaction,
                    "Already set. Use Update birthday.",
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
                self.sharing.enabled,
                sharing=self.sharing,
            )
        self.completed = True
        self.stop()
        saved = dict(
            month=self.month,
            day=self.day,
            timezone=self.zone,
            hour=settings["hour"],
            minute=settings["minute"],
            leap=settings["leap"],
            consent=self.sharing.enabled,
            **self.sharing.record_values(),
        )
        await edit(
            interaction,
            content=None,
            embed=birthday_embed(
                interaction.user,
                saved,
                settings=settings,
                notice="Updated." if self.update else "Saved.",
            ),
            view=navigation(self.bot, self.owner, self.guild_id),
        )
        log.info(
            "birthday_saved guild=%s user=%s action=%s opted_out=%s",
            self.guild_id,
            self.owner,
            "update" if self.update else "set",
            self.opt_out,
        )

    async def cancel(self, interaction):
        self.stop()
        await edit(
            interaction,
            content="Cancelled.",
            view=navigation(self.bot, self.owner, self.guild_id),
        )


class Confirm(OwnedView):
    def __init__(self, owner, callback, *, admin=False, queue=None, bot=None):
        super().__init__(owner, admin=admin, queue=queue)
        self.action = callback
        self.completed = False
        self.bot = bot
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
        await edit(
            interaction,
            content="Birthday deleted.",
            view=navigation(self.bot, self.owner, interaction.guild_id, admin=self.admin),
        )

    async def cancel(self, interaction):
        self.stop()
        await edit(
            interaction,
            content="Cancelled.",
            view=navigation(self.bot, self.owner, interaction.guild_id, admin=self.admin),
        )


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
        self.minute_band = self.minute // 20
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
            placeholder="Setting",
            row=0,
            options=[
                discord.SelectOption(label=label, value=value, default=value == self.section)
                for label, value in self.SECTIONS
            ],
        )

        async def navigate(interaction):
            choice = menu.values[0]
            if choice == "gif":
                await self.edit_gif(interaction)
                return
            self.section = choice
            self.build()
            await edit(interaction, content=self.content(), view=self)

        navigate.opens_modal_values = {"gif"}
        menu.callback = navigate
        self.add_item(menu)
        if self.section in {"channel", "alerts"}:
            channel = discord.ui.ChannelSelect(
                placeholder="Birthday channel" if self.section == "channel" else "Moderator alerts",
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
                placeholder="Birthday list access" if self.section == "privacy" else "February 29",
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
            hours = discord.ui.Select(
                placeholder="Hour (00–23)",
                row=1,
                options=[
                    discord.SelectOption(
                        label=f"Hour {hour:02}", value=str(hour), default=hour == self.hour
                    )
                    for hour in range(24)
                ],
            )

            async def choose_hour(interaction):
                self.hour = int(hours.values[0])
                self.build()
                await edit(interaction, content=self.content(), view=self)

            hours.callback = choose_hour
            self.add_item(hours)
            bands = discord.ui.Select(
                placeholder="Minute range",
                row=2,
                options=[
                    discord.SelectOption(
                        label=f"Minutes {band * 20:02}–{band * 20 + 19:02}",
                        value=str(band),
                        default=band == self.minute_band,
                    )
                    for band in range(3)
                ],
            )

            async def choose_band(interaction):
                self.minute_band = int(bands.values[0])
                self.build()
                await edit(interaction, content=self.content(), view=self)

            bands.callback = choose_band
            self.add_item(bands)
            minutes = discord.ui.Select(
                placeholder="Minute",
                row=3,
                options=[
                    discord.SelectOption(
                        label=f"Minute {minute:02}",
                        value=str(minute),
                        default=minute == self.minute,
                    )
                    for minute in range(self.minute_band * 20, self.minute_band * 20 + 20)
                ],
            )

            async def choose_minute(interaction):
                self.minute = int(minutes.values[0])
                self.build()
                await edit(interaction, content=self.content(), view=self)

            minutes.callback = choose_minute
            self.add_item(minutes)
            self.add_item(button("Midnight (00:00)", self.reset_midnight))
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
            f"Birthday lists: **{'Public' if self.public_birthdays else 'Moderators only'}**\n"
            f"February 29: **{ {'feb28': 'February 28', 'mar1': 'March 1', 'leap_only': 'leap years only'}[self.leap] }**\n\n"
        )

    def make_embed(self, interaction):
        default = getattr(self.bot.config, "gif", DEFAULT_GIF)
        default = gif_url(default) if isinstance(default, str) and default else DEFAULT_GIF
        embed = manager_embed(
            "Birthday bot settings",
            f"[Open GIF]({self.gif or default})",
        )
        for name, value in (
            ("Birthday channel", f"<#{self.channel_id}>" if self.channel_id else "Not set"),
            ("Moderator alerts", f"<#{self.alert_id}>" if self.alert_id else "Off"),
            ("Announcement time", f"**{self.hour:02}:{self.minute:02}** in each member's timezone"),
            ("Birthday lists", "Public" if self.public_birthdays else "Moderators only"),
            (
                "February 29",
                {"feb28": "February 28", "mar1": "March 1", "leap_only": "Leap years only"}[
                    self.leap
                ],
            ),
        ):
            embed.add_field(name=name, value=value, inline=True)
        return embed

    async def edit_gif(self, interaction):
        async def submit(submission, values):
            value = gif_url(values["gif"]) if values["gif"] else ""
            if self.is_finished():
                await private(submission, "Menu expired. Open /birthdaybot.")
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
                queue=self.queue,
                parent=self,
            )
        )

    async def reset_midnight(self, interaction):
        self.hour, self.minute, self.section = 0, 0, "time"
        self.minute_band = 0
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
        embed = self.make_embed(interaction)
        embed.title = "Settings saved"
        await edit(
            interaction,
            content=None,
            embed=embed,
            view=navigation(self.bot, self.owner, self.guild_id, admin=True),
        )
        log.info(
            "guild_configured guild=%s actor=%s channel=%s",
            self.guild_id,
            self.owner,
            self.channel_id,
        )

    async def cancel(self, interaction):
        self.stop()
        await edit(
            interaction,
            content="Cancelled.",
            view=navigation(self.bot, self.owner, self.guild_id, admin=True),
        )
