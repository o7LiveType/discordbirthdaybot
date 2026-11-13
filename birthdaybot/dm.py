"""Independent, bounded and durable birthday direct-message deliveries."""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import discord

from .timeutil import due, next_occurrence

log = logging.getLogger(__name__)


def greeting(server_name, gif, marker):
    name = discord.utils.escape_markdown(discord.utils.escape_mentions(server_name))
    embed = discord.Embed(
        title="🎉 Happy Birthday! 🎂",
        color=0xF6B5D7,
        description=f"Everyone at **{name}** wishes you a wonderful birthday! ✨\n\nHave an amazing day!",
    )
    if gif:
        embed.set_image(url=gif)
    embed.set_footer(text=f"Birthday DM {marker}")
    return embed


class BirthdayDMs:
    def __init__(self, bot, alert):
        self.bot, self.db, self.alert = bot, bot.db, alert

    async def tick(self, now):
        queued = 0
        backlog = False
        for snapshot in await self.db.candidates(now, dm=True):
            if due(snapshot, now) is None or self.bot.get_guild(snapshot["guild_id"]) is None:
                continue
            if queued >= 25:
                backlog = True
                break
            async with self.bot.mutation_lock:
                record = await self.db.birthday(snapshot["guild_id"], snapshot["user_id"])
                settings = await self.db.guild(snapshot["guild_id"])
                target = (
                    due(record, now)
                    if record and record["dm_enabled"] and settings["enabled"]
                    else None
                )
                if target is not None:
                    year = target.astimezone(ZoneInfo(record["timezone"])).year
                    await self.db.claim_dm(record, year, now.timestamp())
                    queued += 1
        jobs = await self.db.read(
            """SELECT d.* FROM dm_deliveries d JOIN guilds g ON g.guild_id=d.guild_id
            WHERE d.state IN ('ready','retry','sending','uncertain') AND d.retry_at<=?
              AND (d.state IN ('sending','uncertain') OR g.enabled=1)
            ORDER BY d.retry_at,d.created_at LIMIT 26""",
            (now.timestamp(),),
        )
        backlog = backlog or len(jobs) > 25
        for snapshot in jobs[:25]:
            async with self.bot.mutation_lock:
                job = await self.db.dm_job(
                    snapshot["guild_id"], snapshot["user_id"], snapshot["year"]
                )
                if not job or job["state"] not in {"ready", "retry", "sending", "uncertain"}:
                    continue
                settings = await self.db.guild(job["guild_id"])
                if job["state"] in {"sending", "uncertain"}:
                    await self.reconcile(job, settings, now)
                    continue
                guild = self.bot.get_guild(job["guild_id"])
                record = await self.db.birthday(job["guild_id"], job["user_id"])
                current = datetime.now(UTC) if self.bot.live_clock else now
                if not guild or not record or not record["dm_enabled"]:
                    await self.db.dm_state(
                        job, "skipped", error="No eligible birthday at delivery time"
                    )
                    continue
                if not settings["enabled"]:
                    continue
                if due(record, current) is None:
                    upcoming = next_occurrence(record, current)
                    zone = ZoneInfo(record["timezone"])
                    if upcoming.astimezone(zone).date() == current.astimezone(zone).date():
                        await self.db.dm_state(job, "ready", retry_at=upcoming.timestamp())
                    else:
                        await self.db.dm_state(job, "skipped", error="Local birthday has ended")
                    continue
                await self.send(record, job, settings, guild, current)
        return backlog

    async def send(self, record, job, settings, guild, now):
        try:
            member = await guild.fetch_member(record["user_id"])
        except discord.NotFound:
            await self.db.dm_state(job, "skipped", error="Member left the server")
            await self.db.forget(guild.id, record["user_id"])
            return
        except (discord.HTTPException, OSError, TimeoutError):
            await self.retry(job, now, "Could not verify server membership")
            return
        if member.bot:
            await self.db.dm_state(job, "skipped", error="Bot account")
            return
        try:
            channel = await member.create_dm()
        except discord.Forbidden:
            await self.blocked(job)
            return
        except (discord.HTTPException, OSError, TimeoutError):
            await self.retry(job, now, "Could not open DM channel")
            return
        current = datetime.now(UTC) if self.bot.live_clock else now
        if due(record, current) is None:
            await self.db.dm_state(job, "skipped", error="Local birthday ended before send")
            return
        # Persist the channel and intent before the external side effect.
        await self.db.dm_state(job, "sending", channel_id=channel.id)
        job = dict(job) | {"channel_id": channel.id}
        try:
            message = await channel.send(
                embed=greeting(guild.name, settings["gif"] or self.bot.config.gif, job["marker"]),
                allowed_mentions=discord.AllowedMentions.none(),
                nonce=job["marker"],
            )
        except discord.Forbidden:
            await self.blocked(job)
            return
        except discord.NotFound:
            await self.db.dm_state(job, "skipped", error="DM recipient unavailable")
            return
        except Exception:
            await self.db.dm_state(job, "uncertain", error="Ambiguous Discord DM send")
            await self.reconcile(job, settings, current)
            return
        await self.confirm(job, message, now=current)
        log.info("birthday_dm_sent guild=%s user=%s message=%s", guild.id, member.id, message.id)

    async def blocked(self, job):
        await self.db.dm_state(job, "blocked", error="Recipient does not accept this bot's DMs")
        log.info("birthday_dm_blocked guild=%s user=%s", job["guild_id"], job["user_id"])

    async def retry(self, job, now, error):
        delay = min(3600, 60 * 2 ** min(job["attempts"], 6))
        await self.db.dm_state(job, "retry", error=error, retry_at=now.timestamp() + delay)
        log.warning(
            "birthday_dm_retry guild=%s user=%s delay=%s", job["guild_id"], job["user_id"], delay
        )

    async def reconcile(self, job, settings, now):
        if job["channel_id"]:
            try:
                channel = self.bot.get_partial_messageable(job["channel_id"])
                async for message in channel.history(
                    limit=500,
                    after=datetime.fromtimestamp(job["created_at"], UTC) - timedelta(seconds=5),
                ):
                    if message.author.id == self.bot.user.id and any(
                        e.footer.text == f"Birthday DM {job['marker']}" for e in message.embeds
                    ):
                        await self.confirm(job, message, now=now)
                        log.info(
                            "birthday_dm_reconciled guild=%s user=%s",
                            job["guild_id"],
                            job["user_id"],
                        )
                        return
            except (discord.HTTPException, OSError, TimeoutError):
                pass
        await self.db.dm_state(
            job,
            "uncertain",
            error="DM send needs review; no automatic resend",
            retry_at=now.timestamp() + 3600,
        )
        await self.alert(
            settings,
            "dm_uncertain",
            "A birthday DM needs review to avoid duplicates. Open /birthdaybot → Server tools → DM deliveries.",
        )

    async def confirm(self, job, message, now=None):
        created = getattr(message, "created_at", None) or now or datetime.now(UTC)
        await self.db.dm_state(
            job, "sent", message_id=message.id, delete_at=created.timestamp() + 86400
        )
