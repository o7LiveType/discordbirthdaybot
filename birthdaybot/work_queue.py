"""Bounded interaction queue: acknowledge quickly, run a few jobs at a time."""

import asyncio
import functools
import logging
import time
from collections import Counter, OrderedDict, deque

log = logging.getLogger(__name__)


class WorkQueue:
    def __init__(self, capacity=64, workers=2, max_wait=20):
        self.jobs = asyncio.Queue(maxsize=capacity)
        self.pending = set()
        self.worker_count = workers
        self.max_wait = max_wait
        self.tasks = []
        self.rates = OrderedDict()
        self.guild_pending = Counter()
        self.last_busy_log = 0

    def admit(self, key):
        now = time.monotonic()
        bucket = self.rates.setdefault(key, deque(maxlen=12))
        if len(bucket) == 12 and now - bucket[0] < 10:
            return False
        bucket.append(now)
        self.rates.move_to_end(key)
        if len(self.rates) > 10000:
            self.rates.popitem(last=False)
        return True

    def start(self):
        self.tasks = [
            asyncio.create_task(self.worker(), name=f"interaction-worker-{i}")
            for i in range(self.worker_count)
        ]

    async def run(self, interaction, callback, *, update=False, rate_checked=False):
        key = (interaction.guild_id, interaction.user.id)
        if (
            (not rate_checked and not self.admit(key))
            or key in self.pending
            or self.jobs.full()
            or self.guild_pending[key[0]] >= 8
        ):
            text = "I'm busy with your last action or the queue is full. Please wait a moment and try again."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
            if time.monotonic() - self.last_busy_log >= 10:
                log.warning("interaction_queue_busy guild=%s depth=%s", key[0], self.jobs.qsize())
                self.last_busy_log = time.monotonic()
            return
        self.pending.add(key)
        self.guild_pending[key[0]] += 1
        future = asyncio.get_running_loop().create_future()
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=not update)
            self.jobs.put_nowait((key, time.monotonic(), callback, future))
        except asyncio.QueueFull:
            self.release(key)
            await interaction.followup.send("The queue is full. Try again shortly.", ephemeral=True)
            return
        except BaseException:
            self.release(key)
            raise
        try:
            return await future
        except TimeoutError:
            await interaction.followup.send(
                "That action timed out. Open /birthdaybot → My birthday before retrying a save.",
                ephemeral=True,
            )

    async def worker(self):
        while True:
            key, created, callback, future = await self.jobs.get()
            try:
                if future.cancelled():
                    continue
                if time.monotonic() - created > self.max_wait:
                    raise TimeoutError("Interaction expired in queue")
                async with asyncio.timeout(15):
                    result = await callback()
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.release(key)
                self.jobs.task_done()

    def release(self, key):
        self.pending.discard(key)
        self.guild_pending[key[0]] -= 1
        if self.guild_pending[key[0]] <= 0:
            del self.guild_pending[key[0]]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        while not self.jobs.empty():
            key, _, _, future = self.jobs.get_nowait()
            self.release(key)
            future.cancel()
            self.jobs.task_done()


def queued(bot):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(interaction, *args, **kwargs):
            return await bot.queue.run(interaction, lambda: function(interaction, *args, **kwargs))

        return wrapped

    return decorate
