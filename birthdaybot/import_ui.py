"""Private, bounded CSV import workflow with cancellable member verification."""

import asyncio
import io
import logging

import discord

from .csv_import import MAX_BYTES, parse_csv
from .ui import OwnedView, button, edit, navigation, private

log = logging.getLogger(__name__)
TEMPLATE = b"user_id,month,day,timezone,opt_out\n"


class ImportUpload(discord.ui.Modal):
    def __init__(self, parent):
        super().__init__(title="Import birthdays from CSV", timeout=300)
        self.parent = parent
        self.upload = discord.ui.FileUpload(min_values=1, max_values=1, required=True)
        self.add_item(
            discord.ui.Label(
                text="CSV file (UTF-8; up to 500 rows / 256 KiB)", component=self.upload
            )
        )

    async def on_submit(self, interaction):
        if not await self.parent.check_access(interaction):
            return

        async def process():
            if not await self.parent.check_access(interaction):
                return
            try:
                if len(self.upload.values) != 1:
                    raise ValueError("Choose exactly one CSV file.")
                file = self.upload.values[0]
                if not file.filename.lower().endswith(".csv") or not 0 < file.size <= MAX_BYTES:
                    raise ValueError("Choose a .csv file no larger than 256 KiB.")
                parsed = parse_csv(await file.read())
            except ValueError as exc:
                await private(interaction, str(exc))
                return
            if not await self.parent.check_access(interaction):
                return
            self.parent.load(interaction, parsed)
            await self.parent.refresh(interaction)

        await self.parent.bot.queue.run(interaction, process, update=True)

    async def on_error(self, interaction, error):
        log.error(
            "csv_upload_failed guild=%s actor=%s type=%s",
            interaction.guild_id,
            interaction.user.id,
            type(error).__name__,
        )
        await private(
            interaction, "The file could not be read. Upload it again; no birthdays were imported."
        )


class ImportView(OwnedView):
    def __init__(self, bot, owner, guild_id):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot, self.guild_id = bot, guild_id
        self.parsed, self.job = None, None
        self.statuses = {}
        self.phase = "empty"
        self.csv_sharing, self.page, self.completed = False, 0, False
        self.reviewed = False
        bot.import_sessions[guild_id] = self
        self.build()

    def stop(self):
        if self.job and not self.job.done():
            self.job.cancel()
        self.parsed = None
        self.statuses.clear()
        if self.bot.import_sessions.get(self.guild_id) is self:
            del self.bot.import_sessions[self.guild_id]
        super().stop()

    async def on_timeout(self):
        self.stop()

    def load(self, interaction, parsed):
        if self.job and not self.job.done():
            self.job.cancel()
        self.job = None
        self.parsed, self.statuses, self.page = parsed, {}, 0
        self.csv_sharing = False
        self.phase = "invalid" if parsed.issues else "verifying"
        if not parsed.issues:
            self.launch(interaction)

    def launch(self, interaction):
        self.phase = "verifying"
        self.job = asyncio.create_task(
            self.verify(interaction), name="birthday-csv-member-verification"
        )
        self.bot.import_tasks.add(self.job)
        self.job.add_done_callback(self.bot.import_tasks.discard)

    def ready(self):
        return (
            self.phase == "ready"
            and self.parsed
            and not self.parsed.issues
            and all(
                self.statuses.get(row.user_id) in {"member", "existing", "left"}
                for row in self.parsed.rows
            )
            and any(state == "member" for state in self.statuses.values())
        )

    def build(self):
        self.clear_items()
        self.add_item(button("Upload CSV", self.upload, row=0, opens_modal=True))
        self.add_item(button("CSV template", self.template, row=0))
        self.add_item(button("Refresh preview", self.refresh, row=0))
        self.add_item(button("Previous", self.previous, row=1, disabled=self.page == 0))
        self.add_item(
            button(
                "Next",
                self.next,
                row=1,
                disabled=not self.parsed
                or (self.page + 1) * 10 >= max(len(self.parsed.rows), len(self.parsed.issues)),
            )
        )
        self.add_item(
            button(
                "CSV sharing: " + ("ON" if self.csv_sharing else "OFF"),
                self.toggle,
                row=2,
                disabled=self.phase == "verifying",
            )
        )
        self.add_item(
            button(
                "Retry verification",
                self.retry,
                row=2,
                disabled=self.phase != "ready" or "unverified" not in self.statuses.values(),
            )
        )
        self.add_item(
            button(
                "Review import",
                self.review,
                row=3,
                style=discord.ButtonStyle.success,
                disabled=not self.ready(),
            )
        )
        self.add_item(button("Cancel / Server tools", self.bot.menu_actions["tools"], row=3))

    def content(self):
        lines = [
            "**Import birthdays · moderators only**",
            "Upload CSV → verify → review → confirm. Existing saved or pending birthdays are always skipped.",
            "Required: `user_id` (or `member_id`), `month`, `day`. Optional: `timezone`, `opt_out` (or `consent`/`sharing`).",
            "Month may be a number or English name. Timezone must be an IANA name, e.g. `America/New_York`. Birth years and extra columns are discarded.",
            "**Private import is the default.** CSV sharing ON honors explicit opt-in values and can enable public lists, birthday posts and DMs. Blank sharing stays OFF.",
            "Missing timezone → pending: hidden, no posts/DMs, and the member confirms their timezone through Set/Update birthday.",
        ]
        if not self.parsed:
            return (
                "\n".join(lines)
                + "\nMaximum 500 rows / 256 KiB per file. Use CSV template for the column names."
            )
        counts = {
            state: list(self.statuses.values()).count(state)
            for state in {"member", "existing", "left", "unverified"}
        }
        lines += [
            f"\n**{self.parsed.total} CSV rows · {len(self.parsed.issues)} errors · {self.phase}**",
            f"Verified members: {counts['member']} · existing: {counts['existing']} · departed/not found: {counts['left']} · unverified: {counts['unverified']}",
            f"Checked: {len(self.statuses)}/{len(self.parsed.rows)} · ignored columns: {self.parsed.ignored_columns}",
        ]
        if self.parsed.issues:
            lines += ["**Fix the CSV errors and upload it again. Nothing has been imported.**"]
            values = self.parsed.issues
            lines += [
                f"Line {issue.line}: {issue.message}"
                for issue in values[self.page * 10 : (self.page + 1) * 10]
            ]
        else:
            lines += [
                f"Line {row.line}: <@{row.user_id}> · {row.month:02}/{row.day:02} · {row.timezone or 'NEEDS TIMEZONE'} · {self.statuses.get(row.user_id, 'waiting')}"
                for row in self.parsed.rows[self.page * 10 : (self.page + 1) * 10]
            ]
        return "\n".join(lines)

    async def refresh(self, interaction):
        self.reviewed = False
        self.build()
        await edit(
            interaction,
            content=self.content(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def upload(self, interaction):
        await interaction.response.send_modal(ImportUpload(self))

    async def template(self, interaction):
        await private(
            interaction,
            "Fill in Discord user IDs, month/day and optional IANA timezone / opt_out. Save as UTF-8 CSV. Blank opt_out stays private; birth years are discarded.",
            file=discord.File(io.BytesIO(TEMPLATE), filename="birthday-import-template.csv"),
        )

    async def previous(self, interaction):
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    async def next(self, interaction):
        if self.parsed:
            self.page = min(
                self.page + 1,
                max(0, (max(len(self.parsed.rows), len(self.parsed.issues)) - 1) // 10),
            )
        await self.refresh(interaction)

    async def toggle(self, interaction):
        self.csv_sharing = not self.csv_sharing
        await self.refresh(interaction)

    async def retry(self, interaction):
        if self.parsed and self.phase == "ready" and "unverified" in self.statuses.values():
            self.statuses = {
                user: state for user, state in self.statuses.items() if state != "unverified"
            }
            self.launch(interaction)
        await self.refresh(interaction)

    async def verify(self, interaction):
        try:
            parsed = self.parsed
            existing = await self.bot.db.import_existing(
                self.guild_id, [row.user_id for row in parsed.rows]
            )
            for row in parsed.rows:
                if self.is_finished():
                    return
                if row.user_id in existing:
                    self.statuses[row.user_id] = "existing"
                elif row.user_id not in self.statuses:
                    try:
                        async with self.bot.import_slots:
                            async with asyncio.timeout(12):
                                member = await interaction.guild.fetch_member(row.user_id)
                        self.statuses[row.user_id] = (
                            "member"
                            if member.id == row.user_id and member.guild.id == self.guild_id
                            else "unverified"
                        )
                    except discord.NotFound:
                        self.statuses[row.user_id] = "left"
                    except (discord.HTTPException, OSError, TimeoutError):
                        self.statuses[row.user_id] = "unverified"
                        for remaining in parsed.rows:
                            self.statuses.setdefault(
                                remaining.user_id,
                                "existing" if remaining.user_id in existing else "unverified",
                            )
                        break
            self.phase = "ready"
            log.info(
                "csv_verified guild=%s actor=%s rows=%s verified=%s",
                self.guild_id,
                self.owner,
                parsed.total,
                len(self.statuses),
            )
            if not self.is_finished():
                await self.refresh(interaction)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.phase = "failed"
            log.error(
                "csv_verification_failed guild=%s actor=%s type=%s",
                self.guild_id,
                self.owner,
                type(exc).__name__,
            )
            if not self.is_finished():
                try:
                    await self.refresh(interaction)
                except discord.HTTPException:
                    pass

    async def review(self, interaction):
        if not self.ready():
            await private(
                interaction, "Finish verification and fix every CSV error before importing."
            )
            return
        rows = [row for row in self.parsed.rows if self.statuses.get(row.user_id) == "member"]
        self.reviewed = True
        pending = sum(row.timezone is None for row in rows)
        enabled = sum(
            bool(self.csv_sharing and row.timezone and row.consent is True) for row in rows
        )
        self.clear_items()
        self.add_item(button("Confirm import", self.confirm, style=discord.ButtonStyle.danger))
        self.add_item(button("Back to preview", self.refresh))
        await edit(
            interaction,
            content=f"**Confirm birthday import**\n{len(rows)} verified members: {len(rows) - pending} saved birthdays, {pending} pending timezone confirmation.\n**{enabled} birthdays will have public sharing, posts and DMs enabled.** All others stay private or pending.\nExisting records and annual anti-spam receipts are preserved. Departed members are skipped. No records have been written yet.",
            view=self,
        )

    async def confirm(self, interaction):
        if not await self.check_access(interaction):
            return
        if self.completed or not self.ready() or not self.reviewed:
            await private(interaction, "This import was already completed or is not ready.")
            return
        rows = [row for row in self.parsed.rows if self.statuses.get(row.user_id) == "member"]
        async with self.bot.mutation_lock:
            saved, pending, skipped = await self.bot.db.import_birthdays(
                self.guild_id, self.owner, rows, csv_sharing=self.csv_sharing
            )
        self.completed = True
        log.info(
            "csv_imported guild=%s actor=%s saved=%s pending=%s raced_skips=%s csv_sharing=%s",
            self.guild_id,
            self.owner,
            saved,
            pending,
            skipped,
            self.csv_sharing,
        )
        await edit(
            interaction,
            content=f"✅ Import complete: **{saved} saved**, **{pending} awaiting timezone confirmation**, **{skipped} newly existing records skipped**. Other existing/departed records were excluded during verification.\nPending members can open Set or Update birthday to confirm their imported details.",
            view=navigation(self.bot, self.owner, self.guild_id, admin=True),
        )
