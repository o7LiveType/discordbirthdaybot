import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import discord

from .dm import BirthdayDMs
from .timeutil import due

log = logging.getLogger(__name__)
REQUIRED = ("view_channel", "send_messages", "embed_links", "read_message_history")


def channel_issues(channel, guild):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        return ["Choose a text channel in this server."]
    if guild.me is None:
        return ["Bot membership is unavailable."]
    permissions = channel.permissions_for(guild.me)
    return [
        f"Missing {name.replace('_', ' ')}." for name in REQUIRED if not getattr(permissions, name)
    ]


def celebration(user_id, gif, marker, *, preview=False, name=None):
    recipient = (
        f"<@{user_id}>"
        if name is None
        else discord.utils.escape_markdown(discord.utils.escape_mentions(name))
    )
    embed = discord.Embed(
        title="🎉 Happy Birthday! 🎂",
        color=0xF6B5D7,
        description=(
            "A little cake, a lot of joy, and a whole server cheering you on.\n\n"
            f"Happy birthday, {recipient}! Have a wonderful day! ✨"
        ),
    )
    if gif:
        embed.set_image(url=gif)
    embed.set_footer(
        text="Preview • no birthday was posted" if preview else f"Birthday delivery {marker}"
    )
    return embed


class Scheduler:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.last_tick = None
        self.last_success = None
        self.dms = BirthdayDMs(bot, self.alert)

    async def alert(self, settings, code, detail):
        if not await self.db.notice_due(settings["guild_id"], code, datetime.now(UTC).timestamp()):
            return
        log.warning("ops_alert guild=%s code=%s detail=%s", settings["guild_id"], code, detail)
        channel = self.bot.get_channel(settings["alert_channel_id"] or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    f"⚠️ **Birthday bot needs attention**\n{detail}\n"
                    "Open `/birthdaybot` → Server tools → Status to investigate. Repeats at most hourly.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.HTTPException, OSError, TimeoutError) as exc:
                log.warning(
                    "ops_alert_failed guild=%s type=%s", settings["guild_id"], type(exc).__name__
                )

    async def run(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.bot.wait_until_ready()
                await self.tick(datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler_tick_failed; will retry next tick")
            await asyncio.sleep(
                1 if getattr(self, "backlog", False) else self.bot.config.poll_seconds
            )

    async def tick(self, now):
        self.last_tick = now
        self.backlog = False
        for job in await self.db.read(
            "SELECT * FROM deliveries WHERE state IN ('sending','uncertain') AND retry_at<=? ORDER BY retry_at LIMIT 10",
            (now.timestamp(),),
        ):
            if self.bot.get_guild(job["guild_id"]) is not None:
                async with self.bot.mutation_lock:
                    await self.reconcile(job, await self.db.guild(job["guild_id"]))
        await self.cleanup_messages(now)
        self.backlog = await self.dms.tick(now) or self.backlog
        settings_by_id = {r["guild_id"]: r for r in await self.db.read("SELECT * FROM guilds")}
        if not hasattr(self, "_health_check") or (now - self._health_check).total_seconds() >= 300:
            for settings in settings_by_id.values():
                guild = self.bot.get_guild(settings["guild_id"])
                if guild is None or not settings["enabled"]:
                    continue
                issues = channel_issues(guild.get_channel(settings["channel_id"] or 0), guild)
                if issues:
                    await self.alert(settings, "configuration", " ".join(issues))
            self._health_check = now
        processed = 0
        for snapshot in await self.db.candidates(now):
            if due(snapshot, now) is None:
                continue
            if processed >= 25:
                self.backlog = True
                break
            guild = self.bot.get_guild(snapshot["guild_id"])
            if guild is None:
                continue
            async with self.bot.mutation_lock:
                record = await self.db.birthday(guild.id, snapshot["user_id"])
                fresh_settings = await self.db.guild(guild.id)
                if not record or not record["announce"] or not fresh_settings["enabled"]:
                    continue
                target = due(record, datetime.now(UTC) if self.bot.live_clock else now)
                if target is None:
                    continue
                year = target.astimezone(ZoneInfo(record["timezone"])).year
                job = await self.db.claim(
                    record, year, fresh_settings["channel_id"] or 0, now.timestamp()
                )
                if job["state"] in {"sent", "skipped"} or job["retry_at"] > now.timestamp():
                    continue
                processed += 1
                if job["state"] in {"sending", "uncertain"}:
                    await self.reconcile(job, fresh_settings)
                else:
                    await self.send(record, job, fresh_settings, guild, now)
        self.last_success = now
        if not hasattr(self, "_heartbeat") or (now - self._heartbeat).total_seconds() >= 300:
            log.info("scheduler_heartbeat tick_ok=true latency_ms=%.0f", self.bot.latency * 1000)
            self._heartbeat = now

    async def reconcile(self, job, settings):
        channel = self.bot.get_channel(job["channel_id"])
        if channel is not None:
            try:
                async for message in channel.history(
                    limit=500,
                    after=datetime.fromtimestamp(job["created_at"], UTC) - timedelta(seconds=5),
                ):
                    if message.author.id == self.bot.user.id and any(
                        embed.footer.text == f"Birthday delivery {job['marker']}"
                        for embed in message.embeds
                    ):
                        await self.confirm_sent(job, message)
                        log.info(
                            "delivery_reconciled guild=%s marker=%s", job["guild_id"], job["marker"]
                        )
                        return
            except (discord.HTTPException, OSError, TimeoutError):
                log.warning("reconciliation_history_unavailable guild=%s", job["guild_id"])
        await self.db.delivery_state(
            job,
            "uncertain",
            error="Unconfirmed Discord send; moderator review required",
            retry_at=datetime.now(UTC).timestamp() + 3600,
        )
        await self.alert(
            settings,
            "uncertain",
            "A delivery needs review to avoid a duplicate birthday post. "
            "Open `/birthdaybot` → Server tools → Channel deliveries to review it.",
        )

    async def send(self, record, job, settings, guild, now):
        channel = guild.get_channel(settings["channel_id"] or 0)
        issues = channel_issues(channel, guild)
        if issues:
            await self.retry(job, now, "Channel permissions unavailable")
            await self.alert(settings, "permissions", " ".join(issues))
            return
        try:
            # REST lookup works without privileged member intent; never ping departed members.
            member = await guild.fetch_member(record["user_id"])
        except discord.NotFound:
            await self.db.forget(guild.id, record["user_id"])
            log.info("departed_member_record_removed guild=%s user=%s", guild.id, record["user_id"])
            return
        except (discord.HTTPException, OSError, TimeoutError):
            await self.retry(job, now, "Could not verify server membership")
            return
        if member.bot:
            await self.db.delivery_state(job, "skipped", error="Bot account")
            return
        # Recheck the local day after membership IO, including a midnight crossing.
        send_now = datetime.now(UTC) if self.bot.live_clock else now
        target = due(record, send_now)
        if target is None:
            return
        # Commit intent BEFORE the external side effect. A crash leaves a reconcilable job.
        await self.db.delivery_state(job, "sending", channel_id=channel.id)
        job = dict(job) | {"channel_id": channel.id}
        ping = bool(record["mention_enabled"])
        name = getattr(member, "display_name", None)
        name = name if isinstance(name, str) else f"Member {member.id}"
        recipient = (
            member.mention
            if ping
            else discord.utils.escape_markdown(discord.utils.escape_mentions(name))
        )
        try:
            message = await channel.send(
                content=f"🎂 {recipient}",
                embed=celebration(
                    member.id,
                    settings["gif"] or self.bot.config.gif,
                    job["marker"],
                    name=None if ping else name,
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=[member] if ping else False,
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
                nonce=job["marker"],
            )
        except (discord.Forbidden, discord.NotFound) as exc:
            await self.retry(job, now, f"Discord rejected send ({exc.status})")
            await self.alert(
                settings,
                "send_rejected",
                "Discord rejected a birthday post. Check channel permissions.",
            )
            return
        except Exception:
            # Network errors and server errors may happen AFTER Discord accepted a post.
            await self.db.delivery_state(job, "uncertain", error="Ambiguous Discord send result")
            log.warning("delivery_uncertain guild=%s marker=%s", guild.id, job["marker"])
            await self.reconcile(job, settings)
            return
        await self.confirm_sent(job, message, now=send_now)
        log.info(
            "delivery_sent guild=%s user=%s marker=%s message=%s lateness_seconds=%.0f",
            guild.id,
            member.id,
            job["marker"],
            message.id,
            (send_now - target).total_seconds(),
        )

    async def retry(self, job, now, error):
        delay = min(3600, 60 * 2 ** min(job["attempts"], 6))
        await self.db.delivery_state(job, "retry", error=error, retry_at=now.timestamp() + delay)
        log.warning(
            "delivery_retry guild=%s marker=%s delay=%s reason=%s",
            job["guild_id"],
            job["marker"],
            delay,
            error,
        )

    async def confirm_sent(self, job, message, now=None):
        created = getattr(message, "created_at", None) or now or datetime.now(UTC)
        await self.db.delivery_state(
            job, "sent", message_id=message.id, delete_at=created.timestamp() + 86400
        )

    async def cleanup_messages(self, now):
        rows = await self.db.read(
            "SELECT * FROM cleanup WHERE delete_at<=? AND retry_at<=? ORDER BY delete_at LIMIT 11",
            (now.timestamp(), now.timestamp()),
        )
        if len(rows) > 10:
            self.backlog = True
        for row in rows[:10]:
            try:
                channel = self.bot.get_partial_messageable(row["channel_id"])
                await channel.get_partial_message(row["message_id"]).delete()
            except discord.NotFound:
                pass  # Already removed or channel deleted.
            except (discord.HTTPException, OSError, TimeoutError):
                delay = min(3600, 60 * 2 ** min(row["attempts"], 6))
                await self.db.cleanup_retry(row["message_id"], now.timestamp() + delay)
                settings = await self.db.guild(row["guild_id"])
                await self.alert(
                    settings,
                    "cleanup_failed",
                    "A birthday post could not be deleted after 24 hours. Check channel access; deletion will retry automatically.",
                )
                continue
            await self.db.cleanup_done(row["message_id"])
            log.info(
                "birthday_post_deleted guild=%s message=%s", row["guild_id"], row["message_id"]
            )
