"""One slash launcher; all member and moderator actions live in private menus."""

import functools
import io
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from .config_export import config_document
from .delivery import celebration, channel_issues
from .directory import open_directory
from .embeds import birthday_date, birthday_embed, manager_embed
from .ui import (
    BirthdayWizard,
    Confirm,
    OwnedView,
    SettingsWizard,
    SharingView,
    button,
    defer,
    edit,
    moderator,
    navigation,
    private,
)
from .work_queue import queued

log = logging.getLogger(__name__)


def require_mod(interaction):
    if not moderator(interaction):
        raise app_commands.MissingPermissions(["manage_guild"])
    return True


async def show(bot, interaction, content, view, *, embed=None):
    bot.track_view(view, interaction.guild_id, interaction.user.id)
    await defer(interaction)
    await edit(
        interaction,
        content=None if embed is not None else content,
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class PrivateList(OwnedView):
    def __init__(self, bot, owner, guild_id):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot, self.guild_id, self.page = bot, guild_id, 0
        self.add_item(button("Previous", self.previous))
        self.add_item(button("Next", self.next))
        self.add_item(button("Server tools", bot.menu_actions["tools"]))

    async def render(self):
        # Requery on every click: don't retain deleted private records in menus.
        total = (
            await self.bot.db.read(
                "SELECT (SELECT count(*) FROM birthdays WHERE guild_id=?)+(SELECT count(*) FROM birthday_import_pending WHERE guild_id=?)",
                (self.guild_id, self.guild_id),
            )
        )[0][0]
        self.page = max(0, min(self.page, (total - 1) // 10))
        values = await self.bot.db.read(
            "SELECT user_id,month,day,timezone,hour,minute,consent,leap,list_public,announce,dm_enabled,mention_enabled,0 AS pending FROM birthdays WHERE guild_id=? UNION ALL SELECT p.user_id,p.month,p.day,p.timezone,g.hour,g.minute,p.consent,g.leap,p.list_public,p.announce,p.dm_enabled,p.mention_enabled,1 FROM birthday_import_pending p JOIN guilds g ON g.guild_id=p.guild_id WHERE p.guild_id=? ORDER BY month,day,user_id LIMIT 10 OFFSET ?",
            (self.guild_id, self.guild_id, self.page * 10),
        )
        lines = [
            f"<@{r['user_id']}> · **{birthday_date(r['month'], r['day'])}** · {r['timezone'] or 'Timezone required'} · "
            f"{r['hour']:02}:{r['minute']:02} · "
            + (
                "Pending"
                if r["pending"]
                else f"List {'On' if r['list_public'] else 'Off'} · Posts {'On' if r['announce'] else 'Off'} · DMs {'On' if r['dm_enabled'] else 'Off'} · Mentions {'On' if r['mention_enabled'] else 'Off'}"
            )
            for r in values
        ]
        return (
            "**Birthday records**\n"
            + ("\n".join(lines) or "No records.")
            + f"\nPage {self.page + 1}/{max(1, (total + 9) // 10)} · {total} records"
        )

    async def previous(self, interaction):
        self.page -= 1
        await defer(interaction)
        await edit(
            interaction,
            content=await self.render(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def next(self, interaction):
        self.page += 1
        await defer(interaction)
        await edit(
            interaction,
            content=await self.render(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class DeliveryList(OwnedView):
    def __init__(self, bot, owner, guild_id, kind):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot, self.guild_id, self.kind = bot, guild_id, kind
        self.page = 0

    @property
    def table(self):
        return "dm_deliveries" if self.kind == "dm" else "deliveries"

    async def render(self):
        rows = await self.bot.db.read(
            f"SELECT * FROM {self.table} WHERE guild_id=? AND state NOT IN ('sent','skipped','blocked') ORDER BY created_at DESC,marker LIMIT 11 OFFSET ?",  # noqa: S608 - fixed table choices
            (self.guild_id, self.page * 10),
        )
        if not rows and self.page:
            self.page = 0
            return await self.render()
        self.clear_items()
        if rows:
            select = discord.ui.Select(
                placeholder="Delivery",
                row=0,
                options=[
                    discord.SelectOption(
                        label=f"{index + 1}. {row['year']} · {row['state']}",
                        description=f"Member ID {row['user_id']}",
                        value=row["marker"],
                    )
                    for index, row in enumerate(rows[:10])
                ],
            )

            async def choose(interaction):
                view = DeliveryReview(
                    self.bot, self.owner, self.guild_id, self.kind, select.values[0]
                )
                await show(self.bot, interaction, await view.render(), view)

            select.callback = choose
            self.add_item(select)
        self.add_item(button("Previous", self.previous, disabled=self.page == 0))
        self.add_item(button("Next", self.next, disabled=len(rows) <= 10))
        self.add_item(button("Refresh", self.refresh))
        self.add_item(button("Server tools", self.bot.menu_actions["tools"]))
        lines = [
            f"**{index + 1}.** <@{row['user_id']}> · {row['year']} · {row['state']}"
            for index, row in enumerate(rows[:10])
        ]
        return (
            f"**{self.kind.title()} deliveries needing attention** · page {self.page + 1}\n"
            + ("\n".join(lines) or "None.")
            + ""
        )

    async def refresh(self, interaction):
        await edit(interaction, content=await self.render(), view=self)

    async def previous(self, interaction):
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    async def next(self, interaction):
        self.page += 1
        await self.refresh(interaction)


class DeliveryReview(OwnedView):
    def __init__(self, bot, owner, guild_id, kind, marker):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot, self.guild_id, self.kind, self.marker = bot, guild_id, kind, marker
        self.before = None
        self.add_item(button("Find posted message", self.find))
        self.add_item(button("Retry or skip…", self.decide))
        self.add_item(button("Back to deliveries", self.back))

    async def job(self):
        table = "dm_deliveries" if self.kind == "dm" else "deliveries"
        rows = await self.bot.db.read(
            f"SELECT * FROM {table} WHERE guild_id=? AND marker=?",  # noqa: S608 - fixed table choices
            (self.guild_id, self.marker),
        )
        return rows[0] if rows else None

    async def render(self):
        job = await self.job()
        if not job:
            return "This delivery no longer exists. Return to deliveries."
        location = (
            "Recipient DM (only delivery metadata is shown)"
            if self.kind == "dm"
            else f"<#{job['channel_id']}>"
        )
        error = discord.utils.escape_markdown((job["error"] or "Queued")[:150])
        return f"**Review {self.kind} delivery**\nMember: <@{job['user_id']}> · {job['year']}\nState: **{job['state']}**\nOriginal location: {location}\nMarker: `{job['marker']}`\nStatus: {error}\nSearch for the bot's matching message first. Retry can duplicate an existing greeting."

    async def back(self, interaction):
        await self.bot.menu_actions[f"{self.kind}_deliveries"](interaction)

    async def find(self, interaction):
        require_mod(interaction)
        job = await self.job()
        if not job or job["state"] not in {"sending", "uncertain", "retry"}:
            await private(
                interaction, "This delivery is no longer awaiting resolution. Refresh the list."
            )
            return
        if not job["channel_id"]:
            await private(
                interaction,
                "No original message channel was recorded. Review the delivery before retrying or skipping.",
            )
            return
        channel = self.bot.get_partial_messageable(job["channel_id"])
        footer = "Birthday DM" if self.kind == "dm" else "Birthday delivery"
        count, oldest = 0, None
        async for message in channel.history(
            limit=500,
            after=datetime.fromtimestamp(job["created_at"], UTC) - timedelta(seconds=5),
            before=discord.Object(self.before) if self.before else None,
            oldest_first=False,
        ):
            count += 1
            oldest = message.id
            if message.author.id == self.bot.user.id and any(
                embed.footer.text == f"{footer} {self.marker}" for embed in message.embeds
            ):
                async with self.bot.mutation_lock:
                    current = await self.job()
                    if not current or current["state"] not in {"sending", "uncertain", "retry"}:
                        await private(
                            interaction,
                            "This delivery changed during the search. Refresh the list.",
                        )
                        return
                    confirm = (
                        self.bot.scheduler.dms.confirm
                        if self.kind == "dm"
                        else self.bot.scheduler.confirm_sent
                    )
                    await confirm(current, message)
                self.audit(interaction, current, "verified_sent")
                await edit(
                    interaction,
                    content="Delivery confirmed. Deletion scheduled after 24 hours.",
                    view=navigation(self.bot, self.owner, self.guild_id, admin=True),
                )
                return
        self.before = oldest if count == 500 else None
        self.children[0].label = "Search older messages" if self.before else "Search again"
        await edit(
            interaction,
            content=await self.render()
            + (
                "\nNo match in this batch of 500."
                if self.before
                else "\nNo match in remaining history."
            ),
            view=self,
        )

    async def decide(self, interaction):
        require_mod(interaction)
        job = await self.job()
        if not job or job["state"] not in {"sending", "uncertain", "retry"}:
            await private(
                interaction, "This delivery is no longer awaiting resolution. Refresh the list."
            )
            return
        view = OwnedView(self.owner, admin=True, queue=self.bot.queue)

        async def retry(submission):
            await self.resolve(submission, "retry")

        async def skip(submission):
            await self.resolve(submission, "skipped")

        async def back(submission):
            # Rebuild a fresh view; show() stops the previous view.
            fresh = DeliveryReview(self.bot, self.owner, self.guild_id, self.kind, self.marker)
            await show(self.bot, submission, await fresh.render(), fresh)

        view.add_item(
            button("Checked for duplicates — retry", retry, style=discord.ButtonStyle.danger)
        )
        view.add_item(button("Skip this greeting", skip))
        view.add_item(button("Cancel", back))
        await show(
            self.bot,
            interaction,
            f"**Confirm delivery decision**\nMember: <@{job['user_id']}> · {job['year']}\nMarker: `{job['marker']}`\n"
            + (
                "Original location: recipient DM\n"
                if self.kind == "dm"
                else f"Original channel: <#{job['channel_id']}>\n"
            )
            + "Check the marker before retrying. Retry may duplicate an existing message.",
            view,
        )

    async def resolve(self, interaction, state):
        require_mod(interaction)
        if state not in {"retry", "skipped"}:
            raise ValueError("Invalid delivery decision.")
        async with self.bot.mutation_lock:
            job = await self.job()
            if not job or job["state"] not in {"sending", "uncertain", "retry"}:
                await private(interaction, "This delivery was already resolved. Refresh the list.")
                return
            change = self.bot.db.dm_state if self.kind == "dm" else self.bot.db.delivery_state
            await change(job, state, error="Manually resolved by moderator")
        self.audit(interaction, job, state)
        await edit(
            interaction,
            content=f"Delivery: {state}.",
            view=navigation(self.bot, self.owner, self.guild_id, admin=True),
        )

    def audit(self, interaction, job, action):
        log.info(
            "delivery_resolved guild=%s user=%s year=%s actor=%s kind=%s action=%s",
            self.guild_id,
            job["user_id"],
            job["year"],
            interaction.user.id,
            self.kind,
            action,
        )


def install(bot):
    bot.menu_actions = {}
    bot.admin_actions = set()

    def action(name, *, admin=False):
        def register(callback):
            @functools.wraps(callback)
            async def guarded(interaction):
                if admin:
                    require_mod(interaction)
                return await callback(interaction)

            bot.menu_actions[name] = guarded
            if admin:
                bot.admin_actions.add(name)
            return guarded

        return register

    async def result(interaction, content, *, admin=False, embed=None):
        await defer(interaction)
        await edit(
            interaction,
            content=content,
            embed=embed,
            view=navigation(bot, interaction.user.id, interaction.guild_id, admin=admin),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @action("home")
    async def home(interaction):
        view = OwnedView(interaction.user.id, queue=bot.queue)
        actions = [
            ("View birthdays", "directory", 0),
            ("Monthly summary", "summary", 0),
            ("Set birthday", "set", 1),
            ("Update birthday", "update", 1),
            ("My birthday", "me", 1),
            ("Delete birthday", "delete", 2),
            ("Sharing", "sharing", 2),
        ]
        if moderator(interaction):
            actions.append(("Server tools", "tools", 2))
        for label, name, row in actions:
            view.add_item(button(label, bot.menu_actions[name], row=row))
        record = await bot.db.birthday(interaction.guild_id, interaction.user.id)
        if record:
            card = birthday_embed(
                interaction.user, record, settings=await bot.db.guild(interaction.guild_id)
            )
            card.title = "Birthday manager"
        else:
            card = manager_embed("Birthday manager", "Birthday not set.")
        await show(
            bot,
            interaction,
            "**Birthday manager**",
            view,
            embed=card,
        )

    async def open_member(interaction, *, update):
        settings = await bot.db.guild(interaction.guild_id)
        record = await bot.db.birthday(interaction.guild_id, interaction.user.id)
        pending = (
            await bot.db.pending_birthday(interaction.guild_id, interaction.user.id)
            if not record
            else None
        )
        if update and not record and not pending:
            await result(interaction, "No birthday set.")
            return
        if not update and record:
            await result(
                interaction,
                "Already set. Use Update birthday.",
            )
            return
        view = BirthdayWizard(
            bot,
            interaction.user.id,
            interaction.guild_id,
            settings,
            record or pending,
            update=update and record is not None,
            timezone_verified=not pending,
        )
        await show(bot, interaction, view.content(), view)

    @action("set")
    async def set_birthday(interaction):
        await open_member(interaction, update=False)

    @action("update")
    async def update_birthday(interaction):
        await open_member(interaction, update=True)

    @action("sharing")
    async def sharing(interaction):
        record = await bot.db.birthday(interaction.guild_id, interaction.user.id)
        record = (
            record
            if record is not None
            else await bot.db.pending_birthday(interaction.guild_id, interaction.user.id)
        )
        if record is None:
            await result(interaction, "No birthday set.")
            return
        view = SharingView(bot, interaction.user.id, interaction.guild_id, record)
        await show(bot, interaction, view.content(), view)

    @action("me")
    async def me(interaction):
        record = await bot.db.birthday(interaction.guild_id, interaction.user.id)
        if not record:
            if await bot.db.pending_birthday(interaction.guild_id, interaction.user.id):
                await open_member(interaction, update=False)
                return
            await result(
                interaction,
                "No birthday set.",
            )
            return
        await result(
            interaction,
            None,
            embed=birthday_embed(
                interaction.user, record, settings=await bot.db.guild(interaction.guild_id)
            ),
        )

    @action("directory")
    async def directory(interaction):
        await open_directory(bot, interaction)

    @action("summary")
    async def summary(interaction):
        await open_directory(bot, interaction, summary=True)

    async def confirm_delete(interaction, member_id, *, admin=False):
        async def remove(submission):
            if admin:
                require_mod(submission)
            async with bot.mutation_lock:
                await bot.db.forget(submission.guild_id, member_id)
            log.info(
                "birthday_deleted guild=%s user=%s actor=%s",
                submission.guild_id,
                member_id,
                submission.user.id,
            )

        view = Confirm(interaction.user.id, remove, admin=admin, queue=bot.queue, bot=bot)
        await show(
            bot,
            interaction,
            f"Delete birthday for <@{member_id}>?",
            view,
        )

    @action("delete")
    async def delete(interaction):
        await confirm_delete(interaction, interaction.user.id)

    @action("tools", admin=True)
    async def tools(interaction):
        view = OwnedView(interaction.user.id, admin=True, queue=bot.queue)
        actions = [
            ("Server settings", "setup", 0),
            ("All birthday records", "records", 0),
            ("Delete member birthday", "forget", 0),
            ("Preview", "preview", 1),
            ("Status", "status", 1),
            ("Pause / resume", "pause", 1),
            ("Channel deliveries", "channel_deliveries", 2),
            ("DM deliveries", "dm_deliveries", 2),
            ("Import birthdays", "import", 3),
            ("Export server config", "export_config", 3),
            ("Main menu", "home", 3),
        ]
        for label, name, row in actions:
            view.add_item(button(label, bot.menu_actions[name], row=row))
        await show(
            bot,
            interaction,
            "**Server tools**",
            view,
        )

    @action("import", admin=True)
    async def import_birthdays(interaction):
        from .import_ui import ImportView

        if interaction.guild_id in bot.import_sessions:
            await private(
                interaction,
                "An import is already open for this server.",
            )
            return
        if len(bot.import_sessions) >= 3:
            await private(
                interaction,
                "Import slots busy. Try again shortly.",
            )
            return
        view = ImportView(bot, interaction.user.id, interaction.guild_id)
        await show(bot, interaction, view.content(), view)

    @action("setup", admin=True)
    async def setup(interaction):
        view = SettingsWizard(bot, interaction.user.id, await bot.db.guild(interaction.guild_id))
        await show(bot, interaction, view.content(), view)

    @action("export_config", admin=True)
    async def export_config(interaction):
        settings = await bot.db.guild(interaction.guild_id)
        require_mod(interaction)
        await private(
            interaction,
            None,
            embed=manager_embed("Server config"),
            file=discord.File(
                io.BytesIO(config_document(settings, bot.config.gif)),
                filename=f"birthdaybot-config-{interaction.guild_id}.json",
            ),
        )
        log.info(
            "guild_config_exported guild=%s actor=%s", interaction.guild_id, interaction.user.id
        )

    @action("records", admin=True)
    async def records(interaction):
        view = PrivateList(bot, interaction.user.id, interaction.guild_id)
        await show(bot, interaction, await view.render(), view)
        log.info("birthdays_listed guild=%s actor=%s", interaction.guild_id, interaction.user.id)

    @action("forget", admin=True)
    async def forget(interaction):
        view = OwnedView(interaction.user.id, admin=True, queue=bot.queue)
        members = discord.ui.UserSelect(placeholder="Member", row=0)

        async def choose(submission):
            require_mod(submission)
            await confirm_delete(submission, members.values[0].id, admin=True)

        members.callback = choose
        view.add_item(members)
        view.add_item(button("Server tools", bot.menu_actions["tools"]))
        await show(
            bot,
            interaction,
            "**Delete member birthday**",
            view,
        )

    @action("pause", admin=True)
    async def pause(interaction):
        settings = await bot.db.guild(interaction.guild_id)
        view = OwnedView(interaction.user.id, admin=True, queue=bot.queue)

        async def change(submission, paused):
            require_mod(submission)
            async with bot.mutation_lock:
                await bot.db.ensure_guild(submission.guild_id)
                await bot.db.pause(submission.guild_id, paused)
            await result(
                submission,
                "Posts and DMs paused." if paused else "Posts and DMs resumed.",
                admin=True,
            )
            log.info(
                "announcements_paused guild=%s actor=%s paused=%s",
                submission.guild_id,
                submission.user.id,
                paused,
            )

        async def stop(submission):
            await change(submission, True)

        async def resume(submission):
            await change(submission, False)

        view.add_item(button("Pause", stop))
        view.add_item(button("Resume", resume))
        view.add_item(button("Server tools", bot.menu_actions["tools"]))
        await show(
            bot,
            interaction,
            f"Posts and DMs: **{'On' if settings['enabled'] else 'Paused'}**",
            view,
        )

    @action("preview", admin=True)
    async def preview(interaction):
        settings = await bot.db.guild(interaction.guild_id)
        issues = channel_issues(
            interaction.guild.get_channel(settings["channel_id"] or 0),
            interaction.guild,
            ping_everyone=bool(settings["ping_everyone"]),
        )
        await result(
            interaction,
            "Preview only. " + (" ".join(issues) if issues else "Channel permissions OK."),
            admin=True,
            embed=celebration(
                interaction.user.id, settings["gif"] or bot.config.gif, "", preview=True
            ),
        )

    @action("status", admin=True)
    async def status(interaction: discord.Interaction):
        await defer(interaction)
        settings = await bot.db.guild(interaction.guild_id)
        counts = await bot.db.read(
            "SELECT state,count(*) AS n FROM deliveries WHERE guild_id=? GROUP BY state",
            (interaction.guild_id,),
        )
        dm_counts = await bot.db.read(
            "SELECT state,count(*) AS n FROM dm_deliveries WHERE guild_id=? GROUP BY state",
            (interaction.guild_id,),
        )
        mode = (await bot.db.read("PRAGMA journal_mode"))[0][0]
        sync = (await bot.db.read("PRAGMA synchronous"))[0][0]
        issues = channel_issues(
            interaction.guild.get_channel(settings["channel_id"] or 0),
            interaction.guild,
            ping_everyone=bool(settings["ping_everyone"]),
        )
        last = bot.scheduler.last_success
        age = (
            f"{(datetime.now(UTC) - last).total_seconds():.0f}s ago"
            if last
            else "not yet completed"
        )
        await result(
            interaction,
            f"**Birthday bot health**\nScheduler last success: {age}\nPoll interval: {bot.config.poll_seconds}s\n"
            f"Gateway latency: {bot.latency * 1000:.0f}ms\nSQLite: {mode.upper()}, synchronous={'FULL' if sync == 2 else sync}\n"
            f"Announcements: {'enabled' if settings['enabled'] else 'PAUSED'}\n"
            f"Birthday lists: {'public' if settings['public_birthdays'] else 'moderators only'}\n"
            f"Everyone ping: {'On' if settings['ping_everyone'] else 'Off'}\n"
            f"Channel: {settings['channel_id'] or 'NOT SET'}\nAlerts: {settings['alert_channel_id'] or 'logs only'}\n"
            f"GIF configured: {bool(settings['gif'] or bot.config.gif)}\n"
            "Deliveries: "
            + (", ".join(f"{r['state']}={r['n']}" for r in counts) or "none")
            + "\nBirthday DMs: "
            + (", ".join(f"{r['state']}={r['n']}" for r in dm_counts) or "none")
            + "\nChecks: "
            + (" ".join(issues) if issues else "channel permissions OK"),
            admin=True,
        )

    @action("channel_deliveries", admin=True)
    async def channel_deliveries(interaction):
        view = DeliveryList(bot, interaction.user.id, interaction.guild_id, "channel")
        await show(bot, interaction, await view.render(), view)

    @action("dm_deliveries", admin=True)
    async def dm_deliveries(interaction):
        view = DeliveryList(bot, interaction.user.id, interaction.guild_id, "dm")
        await show(bot, interaction, await view.render(), view)

    @app_commands.command(name="birthdaybot", description="Open the birthday bot menu")
    @app_commands.guild_only()
    @app_commands.allowed_installs(guilds=True, users=False)
    @queued(bot)
    async def main_menu(interaction: discord.Interaction):
        await bot.menu_actions["home"](interaction)

    bot.tree.add_command(main_menu)
