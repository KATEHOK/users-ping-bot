"""Bootstrap: config, Vault, startup reconciliation, polling + outbox + prune loops.

No import-time side effects: everything below only executes inside main()/_run().
"""

import asyncio
import logging
import signal
import sys
from datetime import timedelta

from aiogram import Bot, exceptions as aiogram_exceptions
from aiogram.client.default import DefaultBotProperties

from . import config as config_module
from . import rendering, vault
from .clock import SYSTEM_CLOCK, Clock, iso
from .config import Config
from .db import Database, open_database
from .delivery import (
    AmbiguousSend,
    ChatMigrated,
    Delivery,
    PermanentSend,
    RateLimited,
    Transport,
    Unauthorized,
    wait_or_stop,
)
from .handlers import Context
from .services import Services
from .telegram import AiogramTransport, PollingFailed, run_polling

logger = logging.getLogger(__name__)

FATAL_PAUSE = 60.0  # keeps restart: unless-stopped from hammering Telegram and Vault
EXIT_FATAL = 1
EXIT_UNAUTHORIZED = 3
PRUNE_KEEP = timedelta(days=2)
PRUNE_INTERVAL = 3600.0

_QUIET_LOGGERS = ("aiohttp", "aiogram", "aiosqlite", "urllib3", "requests")


def configure_logging(level_name: str) -> None:
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level)
    logging.getLogger().setLevel(level)
    for name in _QUIET_LOGGERS:
        # their debug output can carry request URLs (the token) or SQL parameters (names)
        logging.getLogger(name).setLevel(max(level, logging.WARNING))


def _install_stop_handlers(stop: asyncio.Event, signalled: asyncio.Event) -> None:
    """SIGTERM/SIGINT set both events. `signalled` is set by nothing else: serve() sets
    `stop` itself to wind its loops down, so only `signalled` can cut the fatal pause short."""

    def on_signal() -> None:
        signalled.set()
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:
            pass  # signal handlers unsupported on this platform


# --- startup reconciliation and report (decisions section 11) ---


async def reconcile_chats(ctx: Context, transport: Transport) -> list[int]:
    """Probe every active chat. Returns the chat ids unregistered because the bot is gone."""
    async with ctx.db.reader() as c:
        chats = await ctx.services.list_chats(c)

    removed: list[int] = []
    for chat in chats:
        try:
            await transport.probe_chat(chat.chat_id)
        except PermanentSend:
            async with ctx.db.transaction() as c:
                await ctx.services.unregister_chat(c, chat.chat_id, farewell=False)
            removed.append(chat.chat_id)
        except ChatMigrated as exc:
            async with ctx.db.transaction() as c:
                await ctx.services.migrate_chat(c, chat.chat_id, exc.new_chat_id)
        except (AmbiguousSend, RateLimited):
            logger.info("reconcile_skipped chat_id=%s", chat.chat_id)  # network trouble: keep it
    return removed


async def send_startup_report(ctx: Context, removed: list[int]) -> None:
    async with ctx.db.reader() as c:
        root_id = await ctx.services.get_root(c)
        if root_id is None or not await ctx.services.has_private_contact(c, root_id):
            return
        lang = await ctx.services.get_user_lang(c, root_id)
        rows = await ctx.services.list_chats(c)
    for text in rendering.startup_report_text(removed, rows, lang):
        if not await ctx.delivery.send_reply(root_id, text, reply_to=None, thread_id=None):
            break


async def prune_once(ctx: Context) -> int:
    older_than = iso(ctx.clock.now() - PRUNE_KEEP)
    async with ctx.db.transaction() as c:
        return await ctx.services.prune_processed_updates(c, older_than=older_than)


async def prune_loop(ctx: Context, stop: asyncio.Event) -> None:
    while not await wait_or_stop(ctx.clock, stop, PRUNE_INTERVAL):
        try:
            await prune_once(ctx)
        except Exception as exc:
            # e.g. a locked database: try again next interval
            logger.error("prune_error exc=%s", type(exc).__name__)


# --- run ---


def _exit_code(exc: BaseException) -> int:
    if isinstance(exc, (Unauthorized, aiogram_exceptions.TelegramUnauthorizedError)):
        logger.critical("telegram_unauthorized: the bot token was rejected")
        return EXIT_UNAUTHORIZED
    if isinstance(exc, PollingFailed):
        logger.error("polling_failed: too many consecutive getUpdates errors")
    else:
        logger.error("fatal_error exc=%s", type(exc).__name__)
    return EXIT_FATAL


async def serve(
    *,
    db: Database,
    bot: object,
    transport: Transport,
    bot_id: int,
    bot_username: str,
    cooldown: float,
    clock: Clock,
    stop: asyncio.Event,
) -> int:
    """Run until stopped or fatal. Returns the process exit code; the caller owns the fatal pause."""
    services = Services(clock=clock)
    delivery = Delivery(db, services, transport, clock=clock, stop=stop)
    ctx = Context(
        db=db,
        services=services,
        delivery=delivery,
        bot_id=bot_id,
        bot_username=bot_username,
        clock=clock,
        ping_cooldown_seconds=cooldown,
    )

    tasks: list[asyncio.Future] = []
    fatal: BaseException | None = None
    try:
        removed = await reconcile_chats(ctx, transport)
        await send_startup_report(ctx, removed)
        await prune_once(ctx)

        tasks = [
            asyncio.ensure_future(delivery.outbox_loop(stop)),
            asyncio.ensure_future(run_polling(ctx, bot, stop)),  # type: ignore[arg-type]
            asyncio.ensure_future(prune_loop(ctx, stop)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except Exception as exc:
        fatal = exc
    finally:
        # a signal or one failed loop: let the others finish their current short operation
        stop.set()
        for task in tasks:
            if not task.done():
                await asyncio.wait({task})
        for task in tasks:
            if not task.cancelled() and task.exception() is not None and fatal is None:
                fatal = task.exception()

    return 0 if fatal is None else _exit_code(fatal)


async def _run() -> int:
    try:
        cfg = config_module.load_config()
    except config_module.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)  # names the variable only
        return EXIT_FATAL
    configure_logging(cfg.log_level)

    stop = asyncio.Event()
    signalled = asyncio.Event()
    _install_stop_handlers(stop, signalled)
    try:
        token = await asyncio.to_thread(vault.load_bot_token, cfg)
    except Exception as exc:
        logger.error("vault_error exc=%s", type(exc).__name__)
        await wait_or_stop(SYSTEM_CLOCK, signalled, FATAL_PAUSE)  # a signal cuts the pause short
        return EXIT_FATAL

    return await _serve_with_token(cfg, token, stop, signalled)


async def _serve_with_token(
    cfg: Config, token: str, stop: asyncio.Event, signalled: asyncio.Event
) -> int:
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    code = 0
    try:
        try:
            me = await bot.get_me()
            async with open_database(cfg.db_path) as db:
                code = await serve(
                    db=db,
                    bot=bot,
                    transport=AiogramTransport(bot),
                    bot_id=me.id,
                    bot_username=me.username or "",
                    cooldown=cfg.ping_cooldown_seconds,
                    clock=SYSTEM_CLOCK,
                    stop=stop,
                )
        except Exception as exc:
            code = _exit_code(exc)
    finally:
        await bot.session.close()
    if code != 0:
        # resources are already closed; only a real signal ends the pause early
        await wait_or_stop(SYSTEM_CLOCK, signalled, FATAL_PAUSE)
    return code


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI arguments: configuration comes from the environment
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
