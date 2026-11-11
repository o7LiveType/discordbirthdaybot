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

            def request_shutdown(signum):
                logging.getLogger(__name__).info("shutdown_requested signal=%s", signum.name)
                asyncio.create_task(bot.close())

            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(signum, request_shutdown, signum)
                except NotImplementedError:
                    pass
            try:
                await bot.start(config.token)
            finally:
                logging.getLogger(__name__).info(
                    "bot_connection_finished closed=%s", bot.is_closed()
                )
    logging.getLogger(__name__).info("bot_shutdown_complete")


def main():
    os.umask(0o077)
    try:
        config = Config.load()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        configure(config.log_dir, config.log_level, config.token)
        logging.getLogger(__name__).info("bot_process_start pid=%s", os.getpid())
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger(__name__).exception("bot_fatal_exit")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
