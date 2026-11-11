import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from birthdaybot.work_queue import WorkQueue


def interaction(guild, user):
    response = SimpleNamespace(is_done=lambda: False, defer=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(
        guild_id=guild,
        user=SimpleNamespace(id=user),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_spam_burst_is_bounded_and_other_servers_can_enter():
    queue = WorkQueue(capacity=64, workers=2)
    queue.start()
    event = asyncio.Event()
    active = 0
    maximum = 0

    async def work():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await event.wait()
        active -= 1

    jobs = [
        asyncio.create_task(queue.run(interaction(guild, user), work))
        for guild in range(30)
        for user in range(20)
    ]
    for _ in range(4):
        await asyncio.sleep(0)
    assert maximum <= 2
    assert queue.jobs.qsize() <= 64
    assert len(queue.pending) <= 66
    assert max(queue.guild_pending.values()) <= 8
    event.set()
    await asyncio.gather(*jobs)
    assert not queue.pending and not queue.guild_pending
    await queue.close()


async def test_one_pending_job_per_user_and_errors_do_not_kill_workers():
    queue = WorkQueue()
    queue.start()
    event = asyncio.Event()
    first = asyncio.create_task(queue.run(interaction(1, 1), event.wait))
    await asyncio.sleep(0)
    rejected = interaction(1, 1)
    await queue.run(rejected, AsyncMock())
    rejected.response.send_message.assert_awaited_once()
    event.set()
    await first

    async def fail():
        raise ValueError("test")

    import pytest

    with pytest.raises(ValueError):
        await queue.run(interaction(2, 2), fail)
    success = AsyncMock(return_value=123)
    assert await queue.run(interaction(3, 3), success) == 123
    await queue.close()


def test_rate_limit_shared_across_menus_and_bounded_memory():
    queue = WorkQueue()
    assert all(queue.admit((1, 1)) for _ in range(12))
    assert not queue.admit((1, 1))
    for user in range(12000):
        queue.admit((1, user))
    assert len(queue.rates) == 10000
