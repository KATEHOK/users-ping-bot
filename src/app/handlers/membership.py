"""Membership and migration handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import IncomingEvent

if TYPE_CHECKING:
    from . import Context


async def handle_migration(ctx: Context, event: IncomingEvent) -> None:
    """A group -> supergroup service message, seen from either side."""
    from . import mark_ignored

    if event.migrate_to_chat_id is not None:
        old_chat_id, new_chat_id = event.chat_id, event.migrate_to_chat_id
        title = None  # the event chat is the old group: its title says nothing new
    else:
        assert event.migrate_from_chat_id is not None
        old_chat_id, new_chat_id = event.migrate_from_chat_id, event.chat_id
        title = event.chat_title

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return
        result = await ctx.services.migrate_chat(c, old_chat_id, new_chat_id, title=title)
        if result.action in ("noop", "contradictory"):
            await mark_ignored(c, ctx, event.update_id)
    # never announced into the chats: nothing to send
    if result.action in ("moved", "kept_destination"):
        await ctx.delivery.sync_chat_menus([old_chat_id, new_chat_id], single_attempt=True)


async def handle_member_left(ctx: Context, event: IncomingEvent) -> None:
    """A `left_chat_member` service message."""
    await _left(ctx, event, is_bot=event.left_user_id == ctx.bot_id)


async def handle_membership(ctx: Context, event: IncomingEvent) -> None:
    """`my_chat_member` (the bot's own status) or `chat_member` (another member's)."""
    if event.kind == "my_chat_member" and event.bot_added:
        await _joined(ctx, event)
    elif event.kind == "my_chat_member":
        await _left(ctx, event, is_bot=event.bot_removed)
    else:
        await _left(ctx, event, is_bot=False)


async def _joined(ctx: Context, event: IncomingEvent) -> None:
    """The bot joined a group: drop stale menus, give the adder and staff the register menu."""
    from . import mark_ignored

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return
        canonical = await ctx.services.resolve_chat_id(c, event.chat_id)
        await mark_ignored(c, ctx, event.update_id)
    if canonical == event.chat_id and event.chat_type in ("group", "supergroup"):
        # best effort: an outsider's join must not hold up update processing on a 429
        async with ctx.db.reader() as c:
            users = await ctx.services.staff_ids(c)
        if event.user_id is not None:
            users.append(event.user_id)
        await ctx.delivery.sync_chat_menu(event.chat_id, single_attempt=True, users=users)


async def _left(ctx: Context, event: IncomingEvent, *, is_bot: bool) -> None:
    from . import mark_ignored

    gone = False
    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return
        if await ctx.services.resolve_chat_id(c, event.chat_id) != event.chat_id:
            await mark_ignored(c, ctx, event.update_id)  # old id of a migrated group
            return
        if is_bot:
            # unreachable chat: no farewell; subscriptions go with the registration
            result = await ctx.services.unregister_chat(c, event.chat_id, farewell=False)
            gone = result.generation != 0
            await ctx.services.forget_chat_member_menus(c, event.chat_id)
        elif event.left_user_id is not None:
            # only this chat's subscription; a mere restriction change carries no left_user_id
            await ctx.services.unsubscribe(c, event.chat_id, event.left_user_id)
        else:
            await mark_ignored(c, ctx, event.update_id)
    if gone:
        await ctx.delivery.sync_chat_menu(event.chat_id, single_attempt=True)
