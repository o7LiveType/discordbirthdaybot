import asyncio
import contextlib
import logging
import time
import traceback
from collections import OrderedDict, deque

import discord
from discord import app_commands

from .commands import install
from .database import Database
from .delivery import Scheduler
from .ui import private
from .work_queue import WorkQueue

log = logging.getLogger(__name__)


class PrivateTree(app_commands.CommandTree):
    def __init__(self, client):
        super().__init__(client)
        self.requests = OrderedDict()

    async def interaction_check(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await private(interaction, "Use this bot's commands inside a server.")
            return False
        now = time.monotonic()
        key = (interaction.guild_id, interaction.user.id)
        bucket = self.requests.setdefault(key, deque(maxlen=10))
        if len(bucket) == 10 and now - bucket[0] < 30:
            await private(
                interaction, "Please wait a few seconds. Limit: 10 commands per 30 seconds."
            )
            return False
        bucket.append(now)
        self.requests.move_to_end(key)
        if len(self.requests) > 10000:
            self.requests.popitem(last=False)
        return True

    async def on_error(self, interaction, error):
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need Manage Server permission for that command."
            log.warning(
                "permission_denied guild=%s user=%s", interaction.guild_id, interaction.user.id
            )
        elif isinstance(error, app_commands.CommandNotFound):
            message = "That is an old command cached by Discord. Reopen the command picker and use /birthday set or /birthday-admin setup."
            log.warning(
                "stale_command guild=%s name=%s",
                interaction.guild_id,
                (interaction.data or {}).get("name"),
            )
        elif isinstance(original, ValueError):
            message = str(original)
        else:
            log.error(
                "slash_command_failed command=%s guild=%s user=%s error_type=%s traceback=%s",
                interaction.command.qualified_name if interaction.command else "unknown",
                interaction.guild_id,
                interaction.user.id,
                type(original).__name__,
                "".join(traceback.format_tb(original.__traceback__)),
            )
            message = "That command failed. Please try again. A moderator can check the logs."
        await private(interaction, message)


class BirthdayBot(discord.AutoShardedClient):
    def __init__(self, config):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,
            enable_debug_events=False,
        )
        self.config = config
        self.db = Database(config.database)
        self.tree = PrivateTree(self)
        self.mutation_lock = asyncio.Lock()
        self.scheduler = Scheduler(self)
        self.worker = None
        self.db_open = False
        self.close_lock = asyncio.Lock()
        self.live_clock = True
        self.queue = WorkQueue()
        self.active_views = OrderedDict()
        install(self)

    def track_view(self, view, guild_id, user_id):
        key = (guild_id, user_id)
        previous = self.active_views.pop(key, None)
        if previous:
            previous.stop()
        self.active_views[key] = view
        if len(self.active_views) > 1000:
            _, oldest = self.active_views.popitem(last=False)
            oldest.stop()
        return view

    async def setup_hook(self):
        await self.db.open()
        self.db_open = True
        self.queue.start()
        if self.config.test_guild_id:
            guild = discord.Object(id=self.config.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("commands_synced scope=test_guild count=%s guild=%s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("commands_synced scope=global count=%s", len(synced))
        self.worker = asyncio.create_task(self.scheduler.run(), name="birthday-scheduler")

    async def on_ready(self):
        for guild in self.guilds:
            await self.db.ensure_guild(guild.id)
        log.info("bot_ready application=%s guilds=%s", self.user.id, len(self.guilds))

    async def on_guild_join(self, guild):
        await self.db.ensure_guild(guild.id)
        log.info("guild_joined guild=%s", guild.id)

    async def on_guild_remove(self, guild):
        async with self.mutation_lock:
            await self.db.drop_guild(guild.id)
        log.info("guild_removed_records_deleted guild=%s", guild.id)

    async def close(self):
        async with self.close_lock:
            if self.worker:
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker
                self.worker = None
            await self.queue.close()
            for view in self.active_views.values():
                view.stop()
            self.active_views.clear()
            await super().close()
            if self.db_open:
                await self.db.close()
                self.db_open = False
