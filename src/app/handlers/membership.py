"""Membership and migration handlers (plan sections 6, 10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import IncomingEvent

if TYPE_CHECKING:
    from . import Context


async def handle_migration(ctx: Context, event: IncomingEvent) -> None:
    """A group -> supergroup migration service message, from either side.

    Confirmed migration data is processed before the generic bot-removal logic:
    handlers/__init__.handle_event routes migrate_to/from events here first, ahead
    of the bot/anonymous/edited filter and ahead of member_left/chat_member handling.
    """
    if event.migrate_to_chat_id is not None:
        old_chat_id, new_chat_id = event.chat_id, event.migrate_to_chat_id
    else:
        old_chat_id, new_chat_id = event.migrate_from_chat_id, event.chat_id

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return
        await ctx.services.migrate_chat(c, old_chat_id, new_chat_id, title=event.chat_title)
        # applied or conflict: either way nothing here needs a reply, and a conflict
        # is never announced into the disputed chat (plan section 10)

    ctx.delivery.cancel_chat(old_chat_id)


async def handle_member_left(ctx: Context, event: IncomingEvent) -> None:
    """A `left_chat_member` service message (message.kind == "member_left")."""
    is_bot_removal = event.left_user_id == ctx.bot_id

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return
        canonical = await ctx.services.resolve_chat_id(c, event.chat_id)
        if canonical != event.chat_id:
            # a leave event on a stale, already-migrated id: never touches the new chat
            return
        if is_bot_removal:
            # bot removed: unregister and clear subscriptions, no attempt to message
            # an unreachable chat (no farewell queued)
            await ctx.services.unregister_chat(c, event.chat_id, farewell=False)
        elif event.left_user_id is not None:
            # a subscriber leaving clears only that chat's subscription
            await ctx.services.unsubscribe(c, event.chat_id, event.left_user_id)

    if is_bot_removal:
        ctx.delivery.cancel_chat(event.chat_id)


async def handle_membership(ctx: Context, event: IncomingEvent) -> None:
    """`my_chat_member` (the bot's own status) or `chat_member` (another member's)."""
    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return
        canonical = await ctx.services.resolve_chat_id(c, event.chat_id)
        if canonical != event.chat_id:
            return  # stale id: migration already moved this chat elsewhere

        if event.kind == "my_chat_member":
            if event.bot_removed:
                await ctx.services.unregister_chat(c, event.chat_id, farewell=False)
            # being re-added (bot_removed is False) restores nothing; silence until
            # a new /upb chat register
        elif event.left_user_id is not None:
            # a mere restriction change (no left_user_id) never unsubscribes anyone
            await ctx.services.unsubscribe(c, event.chat_id, event.left_user_id)

    if event.kind == "my_chat_member" and event.bot_removed:
        ctx.delivery.cancel_chat(event.chat_id)
