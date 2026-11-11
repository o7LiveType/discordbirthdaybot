"""Pterodactyl startup: python -u main.py (Python 3.12)."""

import asyncio
import logging
import os
import signal
import sys

from birthdaybot.bot import BirthdayBot
from birthdaybot.config import Config
from birthdaybot.logging_setup import configure


async def run(config):
    # Local Linux/Windows process lock prevents accidental double launches using
    # the same database. A single replica is required even across containers.
    from birthdaybot.process_lock import process_lock

    with process_lock(config.database.with_suffix(".lock")):
        async with BirthdayBot(config) as bot:
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(signum, lambda: asyncio.create_task(bot.close()))
                except NotImplementedError:
                    pass
            await bot.start(config.token)


def main():
    os.umask(0o077)
    try:
        config = Config.load()
        configure(config.log_dir, config.log_level, config.token)
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        logging.getLogger(__name__).exception("bot_fatal_exit")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
