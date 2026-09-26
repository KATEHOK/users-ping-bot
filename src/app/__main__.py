"""Bootstrap: load config, log in to Vault, run the polling loop and the outbox loop.

No import-time side effects: everything below only executes inside main()/_run().
"""

import asyncio
import logging
import signal
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from . import config as config_module
from . import vault
from .clock import SYSTEM_CLOCK
from .db import open_database
from .delivery import Delivery
from .handlers import Context
from .services import Services
from .telegram import AiogramTransport, run_polling

logger = logging.getLogger(__name__)


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # signal handlers unsupported on this platform


async def _run() -> None:
    cfg = config_module.load_config()
    logging.basicConfig(level=cfg.log_level)

    token = vault.load_bot_token(cfg)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        me = await bot.get_me()

        async with open_database(cfg.db_path) as db:
            services = Services(clock=SYSTEM_CLOCK)
            transport = AiogramTransport(bot)
            delivery = Delivery(db, services, transport, clock=SYSTEM_CLOCK)
            ctx = Context(
                db=db,
                services=services,
                delivery=delivery,
                bot_id=me.id,
                bot_username=me.username or "",
                clock=SYSTEM_CLOCK,
            )

            stop = asyncio.Event()
            _install_stop_handlers(stop)

            outbox_task = asyncio.ensure_future(delivery.outbox_loop(stop))
            polling_task = asyncio.ensure_future(run_polling(ctx, bot, stop))
            try:
                await asyncio.wait(
                    {outbox_task, polling_task}, return_when=asyncio.FIRST_EXCEPTION
                )
            finally:
                # SIGTERM/SIGINT (or one loop failing) both converge here: signal the
                # other loop to finish its current short operation, then wait for both
                stop.set()
                for task in (outbox_task, polling_task):
                    if not task.done():
                        await task
                for task in (outbox_task, polling_task):
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        raise exc
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI arguments: configuration comes from the environment
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("fatal_error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
