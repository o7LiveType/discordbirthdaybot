"""Private, bounded CSV import workflow with cancellable member verification."""

import asyncio
import io
import logging
from dataclasses import replace

import discord

from .csv_import import MAX_BYTES, ImportIssue, ImportSharing, parse_csv
from .sharing import FLAGS, LABELS
from .ui import OwnedView, button, edit, navigation, private

log = logging.getLogger(__name__)
TEMPLATE = b"user_id,month,day,timezone,opt_out,list_public,announce,dm_enabled,mention_enabled\n"
MODES = {"off": "Off", "on": "On", "csv": "From CSV"}


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
        await private(interaction, "File unreadable. Upload it again.")


class ImportView(OwnedView):
    def __init__(self, bot, owner, guild_id):
        super().__init__(owner, admin=True, queue=bot.queue)
        self.bot, self.guild_id = bot, guild_id
        self.parsed, self.job = None, None
        self.statuses = {}
        self.phase = "empty"
        self.sharing = ImportSharing()
        self.sharing_draft = None
        self.page, self.completed = 0, False
        self.issues_only = False
        self.edit_lock = asyncio.Lock()
        self.reviewed = False
        bot.import_sessions[guild_id] = self
        self.build()

    def stop(self):
        if self.job and not self.job.done():
            self.job.cancel()
        self.parsed = None
        self.sharing_draft = None
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
        self.issues_only = False
        self.sharing_draft = None
        self.reviewed = False
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
            and all(self.statuses.get(row.user_id) == "member" for row in self.parsed.rows)
        )

    def can_retry(self):
        return bool(
            self.parsed
            and not self.parsed.issues
            and self.phase in {"ready", "blocked", "failed"}
            and not self.ready()
        )

    def preview_items(self):
        if self.parsed is None:
            return ()
        errors, remaining = list(self.parsed.issues), []
        for row in self.parsed.rows:
            if self.issues_only and (
                self.statuses.get(row.user_id) == "member"
                or (self.parsed.issues and not self.row_error(row))
            ):
                continue
            (errors if self.row_error(row) else remaining).append(row)
        return tuple(sorted(errors, key=lambda item: item.line) + remaining)

    def row_error(self, row):
        state = self.statuses.get(row.user_id)
        return state in {"left", "unverified"} or (self.phase == "failed" and state != "member")

    def blocking_reason(self):
        if self.parsed is None:
            return ""
        if self.parsed.issues:
            return "Review blocked: fix the CSV errors and upload again."
        if self.phase == "verifying":
            return "Verification in progress. Refresh preview to check progress."
        if self.phase == "failed":
            return "Verification failed. Use Retry verification."
        reasons = []
        left = sum(state == "left" for state in self.statuses.values())
        unchecked = sum(
            self.statuses.get(row.user_id) not in {"member", "left"} for row in self.parsed.rows
        )
        if left:
            reasons.append(f"{left} not in server: check IDs or remove these rows")
        if unchecked:
            reasons.append(f"{unchecked} unchecked: use Retry verification")
        return "Review blocked: " + "; ".join(reasons) + "." if reasons else ""

    def build(self):
        self.clear_items()
        if self.sharing_draft is not None:
            for row, (key, label) in enumerate(zip(FLAGS, LABELS, strict=True)):
                selected = getattr(self.sharing_draft, key)
                select = discord.ui.Select(
                    placeholder=f"{label}: {MODES[selected]}",
                    row=row,
                    options=[
                        discord.SelectOption(
                            label=f"{label}: {label_mode}", value=mode, default=mode == selected
                        )
                        for mode, label_mode in MODES.items()
                    ],
                )

                async def choose(interaction, key=key, select=select):
                    if self.sharing_draft is None:
                        await private(interaction, "Sharing menu closed.")
                        return
                    self.sharing_draft = replace(self.sharing_draft, **{key: select.values[0]})
                    await self.refresh(interaction)

                select.callback = choose
                self.add_item(select)
            self.add_item(
                button("Apply", self.apply_sharing, row=4, style=discord.ButtonStyle.success)
            )
            self.add_item(button("Back", self.cancel_sharing, row=4))
            self.add_item(button("All off", self.private_sharing, row=4))
            return
        count = len(self.preview_items())
        self.page = max(0, min(self.page, max(0, (count - 1) // 10)))
        self.add_item(button("Upload CSV", self.upload, row=0, opens_modal=True))
        self.add_item(button("CSV template", self.template, row=0))
        self.add_item(button("Refresh preview", self.refresh, row=0))
        self.add_item(button("Previous", self.previous, row=1, disabled=self.page == 0))
        self.add_item(
            button(
                "Next",
                self.next,
                row=1,
                disabled=(self.page + 1) * 10 >= count,
            )
        )
        if self.parsed:
            self.add_item(
                button(
                    "Show all rows" if self.issues_only else "Show issues",
                    self.toggle_issues,
                    row=1,
                )
            )
        self.add_item(
            button(
                "Sharing",
                self.open_sharing,
                row=2,
                disabled=self.phase == "verifying",
            )
        )
        self.add_item(
            button(
                "Retry verification",
                self.retry,
                row=2,
                disabled=not self.can_retry(),
            )
        )
        self.add_item(
            button(
                "Review import",
                self.review,
                row=3,
                style=discord.ButtonStyle.success,
                disabled=self.parsed is None,
            )
        )
        self.add_item(button("Cancel / Server tools", self.bot.menu_actions["tools"], row=3))

    def content(self):
        if self.sharing_draft is not None:
            return "**Import sharing**\nOff: disabled. On: enabled unless CSV opts out.\nFrom CSV: use flags; blank stays off.\n`opt_out=true` disables all four."
        lines = ["**Import birthdays**"]
        sharing = " · ".join(
            f"{label}: {MODES[getattr(self.sharing, key)]}"
            for key, label in zip(FLAGS, LABELS, strict=True)
        )
        if not self.parsed:
            return "\n".join(
                lines
                + [
                    sharing,
                    "CSV · UTF-8 · 500 rows · 256 KiB",
                    "Required: `user_id`, `month`, `day`",
                    "Optional: `timezone` (IANA), `opt_out`, `list_public`, `announce`, `dm_enabled`, `mention_enabled`",
                    "Sharing flags: true/false. `opt_out=true` disables all four.",
                    "Replaces this server's complete birthday list. Missing rows are removed.",
                    "Missing timezone → pending. Header-only CSV clears the list.",
                ]
            )
        reason = self.blocking_reason()
        if reason and self.phase != "verifying":
            lines.append(f"❌ {reason}")
        errors, remaining = [], []
        for item in self.preview_items()[self.page * 10 : (self.page + 1) * 10]:
            if isinstance(item, ImportIssue):
                errors.append(f"❌ Line {item.line}: {item.message}")
            elif self.row_error(item):
                errors.append(self.row_content(item))
            else:
                remaining.append(self.row_content(item))
        lines.extend(errors)
        lines.append(sharing)
        if reason and self.phase == "verifying":
            lines.append(reason)
        if self.issues_only:
            lines.append("Showing blocking rows only.")
        counts = {
            state: list(self.statuses.values()).count(state)
            for state in {"member", "left", "unverified"}
        }
        lines += [
            f"{self.parsed.total} rows · {len(self.parsed.issues) + sum(self.row_error(row) for row in self.parsed.rows)} errors · {self.phase}",
            f"Verified members: {counts['member']} · not in server: {counts['left']} · unverified: {counts['unverified']}",
            f"Checked: {len(self.statuses)}/{len(self.parsed.rows)} · ignored columns: {self.parsed.ignored_columns}",
        ]
        lines.extend(remaining)
        return "\n".join(lines)

    def row_content(self, row):
        state = self.statuses.get(row.user_id, "waiting")
        if self.row_error(row):
            reason = (
                "Not in this server. Check the ID or remove this row."
                if state == "left"
                else "Verification failed. Retry verification."
            )
            return f"❌ Line {row.line}: <@{row.user_id}> · {reason}"
        line = f"Line {row.line}: <@{row.user_id}> · {row.month:02}/{row.day:02} · {row.timezone or 'Timezone required'} · {state}\n"
        return line + " · ".join(
            f"{label}: {'On' if enabled else 'Off'}"
            for label, enabled in zip(
                ("Public", "Posts", "DMs", "Mentions"),
                self.sharing.resolve(row).values(),
                strict=True,
            )
        )

    async def refresh(self, interaction, *, background=False):
        async with self.edit_lock:
            if self.is_finished() or (background and self.reviewed):
                return
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
            "UTF-8 CSV. Sharing flags: true/false; blank stays off. opt_out=true disables all sharing.",
            file=discord.File(io.BytesIO(TEMPLATE), filename="birthday-import-template.csv"),
        )

    async def previous(self, interaction):
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    async def next(self, interaction):
        if self.parsed:
            self.page = min(
                self.page + 1,
                max(0, (len(self.preview_items()) - 1) // 10),
            )
        await self.refresh(interaction)

    async def toggle_issues(self, interaction):
        self.issues_only = not self.issues_only
        self.page = 0
        await self.refresh(interaction)

    async def open_sharing(self, interaction):
        if self.phase == "verifying":
            await private(interaction, "Wait for verification to finish.")
            return
        self.sharing_draft = self.sharing
        await self.refresh(interaction)

    async def apply_sharing(self, interaction):
        if self.sharing_draft is None:
            await private(interaction, "Sharing menu closed.")
            return
        self.sharing = self.sharing_draft
        self.sharing_draft = None
        await self.refresh(interaction)

    async def cancel_sharing(self, interaction):
        self.sharing_draft = None
        await self.refresh(interaction)

    async def private_sharing(self, interaction):
        if self.sharing_draft is None:
            await private(interaction, "Sharing menu closed.")
            return
        self.sharing_draft = ImportSharing.all("off")
        await self.refresh(interaction)

    async def retry(self, interaction):
        if self.can_retry():
            self.statuses = {
                user: state for user, state in self.statuses.items() if state == "member"
            }
            self.launch(interaction)
        await self.refresh(interaction)

    async def verify(self, interaction):
        try:
            parsed = self.parsed
            for row in parsed.rows:
                if self.is_finished():
                    return
                if row.user_id not in self.statuses:
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
                            self.statuses.setdefault(remaining.user_id, "unverified")
                        break
            verified = sum(state == "member" for state in self.statuses.values())
            self.phase = "ready" if verified == len(parsed.rows) else "blocked"
            log.info(
                "csv_verified guild=%s actor=%s rows=%s verified=%s phase=%s",
                self.guild_id,
                self.owner,
                parsed.total,
                verified,
                self.phase,
            )
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
                await self.refresh(interaction, background=True)
            except Exception as exc:
                log.warning(
                    "csv_preview_update_failed guild=%s actor=%s type=%s",
                    self.guild_id,
                    self.owner,
                    type(exc).__name__,
                )

    async def review(self, interaction):
        if self.parsed is None or self.sharing_draft is not None:
            await private(
                interaction,
                "Upload a CSV and close the Sharing menu first.",
            )
            return
        if not self.ready():
            self.issues_only, self.page = False, 0
            await self.refresh(interaction)
            return
        async with self.edit_lock:
            if not self.is_finished():
                await self._show_review(interaction)

    async def _show_review(self, interaction):
        self.issues_only = False
        rows = [row for row in self.parsed.rows if self.statuses.get(row.user_id) == "member"]
        plan = await self.bot.db.import_plan(self.guild_id, [row.user_id for row in rows])
        self.reviewed = True
        pending = sum(row.timezone is None for row in rows)
        resolved = [(row.timezone is not None, self.sharing.resolve(row)) for row in rows]
        counts = []
        for key, label in zip(FLAGS, LABELS, strict=True):
            active = sum(
                complete and getattr(permissions, key) for complete, permissions in resolved
            )
            deferred = sum(
                not complete and getattr(permissions, key) for complete, permissions in resolved
            )
            counts.append(f"{label}: {active} · pending: {deferred}")
        settings = await self.bot.db.guild(self.guild_id)
        counts.append(f"Everyone ping: {'On' if settings['ping_everyone'] else 'Off'}")
        if not settings["enabled"]:
            counts.append("Posts and DMs: Paused")
        if not settings["public_birthdays"]:
            counts.append("Public lists: Moderators only")
        self.clear_items()
        self.add_item(
            button("Replace birthday list", self.confirm, style=discord.ButtonStyle.danger)
        )
        self.add_item(button("Back to preview", self.refresh))
        await edit(
            interaction,
            content="**Replace birthday list**\n"
            + f"Add: {plan.added} · Update: {plan.updated} · Remove: {plan.removed}\n"
            + f"Saved: {len(rows) - pending} · Pending timezone: {pending}\n"
            + "\n".join(counts)
            + "\nOverwrites dates, timezones and sharing. Removes birthdays absent from this CSV.",
            view=self,
        )

    async def confirm(self, interaction):
        if not await self.check_access(interaction):
            return
        if (
            self.completed
            or not self.ready()
            or not self.reviewed
            or self.sharing_draft is not None
        ):
            await private(interaction, "Import completed or not ready.")
            return
        rows = [row for row in self.parsed.rows if self.statuses.get(row.user_id) == "member"]
        async with self.bot.mutation_lock:
            if not await self.check_access(interaction):
                return
            result = await self.bot.db.import_birthdays(
                self.guild_id,
                self.owner,
                rows,
                sharing=self.sharing,
            )
        self.completed = True
        log.info(
            "csv_imported guild=%s actor=%s saved=%s pending=%s added=%s updated=%s removed=%s sharing=%s",
            self.guild_id,
            self.owner,
            result.saved,
            result.pending,
            result.added,
            result.updated,
            result.removed,
            {key: getattr(self.sharing, key) for key in FLAGS},
        )
        await edit(
            interaction,
            content=f"**Birthday list replaced**\nSaved: {result.saved} · Pending timezone: {result.pending}\nAdded: {result.added} · Updated: {result.updated} · Removed: {result.removed}",
            view=navigation(self.bot, self.owner, self.guild_id, admin=True),
        )
