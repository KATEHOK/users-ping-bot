"""Dispatch entrypoint: Context + handle_event, running the section-9 order.

group.py, private.py and membership.py hold the per-kind logic; every state
change goes through services.py, every outgoing call goes through delivery.py.
"""

from dataclasses import dataclass

from ..clock import Clock
from ..db import Database
from ..delivery import Delivery
from ..models import IncomingEvent
from ..services import Services


@dataclass
class Context:
    db: Database
    services: Services
    delivery: Delivery
    bot_id: int
    bot_username: str
    clock: Clock


# imported after Context is defined: group/private/membership only need Context for
# type annotations (under `from __future__ import annotations`), so this ordering
# avoids a real circular-import cycle between the package and its submodules.
from . import group, membership, private  # noqa: E402


async def handle_event(ctx: Context, event: IncomingEvent) -> None:
    if event.kind == "message":
        # migration service messages carry no text and no identified acting user,
        # so they are dispatched before the bot/anonymous/edited filter below
        if event.migrate_to_chat_id is not None or event.migrate_from_chat_id is not None:
            await membership.handle_migration(ctx, event)
            return
        # step 1 (plan section 9): reject anything not from an identified human user,
        # and never re-run a command from an edited message
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
        return
