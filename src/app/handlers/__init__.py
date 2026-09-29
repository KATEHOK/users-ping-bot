"""Dispatch entrypoint: Context + handle_event.

Each handler runs one write transaction, in this order:
1. Parse. A group message that is not a /upb command is dropped before any write.
2. claim_update (a duplicate returns at once), then the sender's contact record.
3. Group only: canonical chat id (an old migrated id is ignored) and chat row.
4. Actor load and the single authorization check; a denial is silent and recorded 'ignored'.
5. Argument validation, mutation, outbox rows.
Replies are sent only after commit and are never re-checked.
"""

import logging
from dataclasses import dataclass, field

import aiosqlite

from ..clock import Clock
from ..db import Database
from ..delivery import Delivery, Unauthorized
from ..models import IncomingEvent
from ..services import Services

logger = logging.getLogger(__name__)


@dataclass
class Context:
    db: Database
    services: Services
    delivery: Delivery
    bot_id: int
    bot_username: str
    clock: Clock
    ping_cooldown_seconds: float = 5.0
    # (canonical chat_id, user_id) -> unix time of the last accepted ping; memory only
    ping_last: dict[tuple[int, int], float] = field(default_factory=dict)


# imported after Context is defined: the submodules only need it for annotations
from . import group, membership, private  # noqa: E402


async def mark_ignored(c: aiosqlite.Connection, ctx: Context, update_id: int) -> None:
    await ctx.services.set_update_outcome(c, ctx.bot_id, update_id, "ignored")


async def _dispatch(ctx: Context, event: IncomingEvent) -> None:
    if event.kind == "message":
        # migration service messages carry no text and no identified sender
        if event.migrate_to_chat_id is not None or event.migrate_from_chat_id is not None:
            await membership.handle_migration(ctx, event)
            return
        # only identified humans; an edited message never re-runs a command
        if event.is_bot or event.user_id is None or event.edited:
            return
        if event.chat_type == "private":
            await private.handle(ctx, event)
        elif event.chat_type in ("group", "supergroup"):
            await group.handle(ctx, event)
        return

    if event.kind == "member_left":
        await membership.handle_member_left(ctx, event)
        return

    if event.kind in ("my_chat_member", "chat_member"):
        await membership.handle_membership(ctx, event)


async def handle_event(ctx: Context, event: IncomingEvent) -> None:
    """Never raises (except a fatal Unauthorized): a poison update is recorded as 'error'."""
    try:
        await _dispatch(ctx, event)
    except Unauthorized:
        raise
    except Exception as exc:
        # the handler transaction has already rolled back; class only, never str(exc)
        logger.error("handler_error update_id=%s exc=%s", event.update_id, type(exc).__name__)
        try:
            async with ctx.db.transaction() as c:
                await ctx.services.record_update_outcome(c, ctx.bot_id, event.update_id, "error")
        except Exception as err:
            logger.error("error_record_failed exc=%s", type(err).__name__)
