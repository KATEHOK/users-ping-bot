"""Group command handlers (plan sections 4, 6, 9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

from .. import access, commands, rendering
from ..models import Actor, Cmd, IncomingEvent, Scope
from ..services import ChatRow

if TYPE_CHECKING:
    from . import Context

ALREADY_REGISTERED_TEXT = "This chat is already registered."
SUBSCRIBED_TEXT = "You are now subscribed to pings in this chat."
UNSUBSCRIBED_TEXT = "You will no longer receive pings in this chat."


@dataclass(slots=True)
class _Outcome:
    kind: str
    payload: Any = None


async def _authorize(
    ctx: Context, c: aiosqlite.Connection, event: IncomingEvent, cmd: Cmd
) -> tuple[bool, Actor, ChatRow | None, int]:
    chat_id = await ctx.services.resolve_chat_id(c, event.chat_id)
    blocked = await ctx.services.is_blocked(c, chat_id)
    chat_row = None if blocked else await ctx.services.get_chat(c, chat_id)
    chat_active = chat_row is not None
    actor = await ctx.services.load_actor(c, event.user_id, chat_id=chat_id)
    ok = (not blocked) and access.can_run(cmd, actor, scope=Scope.GROUP, chat_active=chat_active)
    return ok, actor, chat_row, chat_id


async def handle(ctx: Context, event: IncomingEvent) -> None:
    parsed = commands.parse_group_command(event.text, event.entities, bot_username=ctx.bot_username)
    if parsed is None:
        return  # not a recognised /upb subcommand: no permission ever hinges on it

    cmd = parsed.cmd
    outcome: _Outcome | None = None

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return  # duplicate: complete no-op, not even the contact touch below

        await ctx.services.touch_user(
            c, event.user_id, username=event.username, display_name=event.display_name
        )

        # the only check that matters is the one taken right here, inside this
        # transaction, immediately before mutating (plan section 9's last-check rule)
        ok, actor, chat_row, chat_id = await _authorize(ctx, c, event, cmd)
        if not ok:
            return  # silent path: claim_update + contact touch still commit

        outcome = await _execute(ctx, c, cmd, event, chat_id, actor, chat_row)

    if outcome is not None:
        await _deliver(ctx, event, outcome)


async def _execute(
    ctx: Context,
    c: aiosqlite.Connection,
    cmd: Cmd,
    event: IncomingEvent,
    chat_id: int,
    actor: Actor,
    chat_row: ChatRow | None,
) -> _Outcome:
    if cmd is Cmd.CHAT_REGISTER:
        result = await ctx.services.register_chat(c, chat_id, event.chat_title, event.user_id)
        return _Outcome("register", result)

    if cmd is Cmd.CHAT_UNREGISTER:
        await ctx.services.unregister_chat(c, chat_id)  # farewell queued by services itself
        return _Outcome("unregister", chat_id)

    if cmd is Cmd.NOTIFY_ON:
        result = await ctx.services.subscribe(c, chat_id, event.user_id)
        return _Outcome("notify_on", result)

    if cmd is Cmd.NOTIFY_OFF:
        removed = await ctx.services.unsubscribe(c, chat_id, event.user_id)
        generation = chat_row.registration_generation if chat_row is not None else 0
        grant = ctx.delivery.grants.issue(
            update_id=event.update_id,
            user_id=event.user_id,
            chat_id=chat_id,
            message_id=event.message_id,
            generation=generation,
        )
        return _Outcome("notify_off", (removed, grant))

    if cmd is Cmd.PING:
        snapshot = await ctx.services.list_subscribers(c, chat_id)
        generation = chat_row.registration_generation if chat_row is not None else 0
        return _Outcome("ping", (snapshot, generation))

    if cmd is Cmd.LIST:
        snapshot = await ctx.services.list_subscribers(c, chat_id)
        return _Outcome("list", snapshot)

    if cmd is Cmd.HELP:
        return _Outcome("help", actor)

    raise AssertionError(f"unhandled group command {cmd!r}")


async def _deliver(ctx: Context, event: IncomingEvent, outcome: _Outcome) -> None:
    # replies always go back to the chat/message the command actually arrived in,
    # even though the mutation above may have applied to a resolved canonical id
    chat_id = event.chat_id
    reply_to = event.message_id
    thread_id = event.thread_id

    if outcome.kind == "register":
        result = outcome.payload
        text = rendering.welcome_text() if result.created else ALREADY_REGISTERED_TEXT
        await ctx.delivery.send_reply(chat_id, text, reply_to=reply_to, thread_id=thread_id)

    elif outcome.kind == "unregister":
        ctx.delivery.cancel_chat(outcome.payload)
        # the farewell itself is a queued outbox event, delivered by the outbox loop

    elif outcome.kind == "notify_on":
        ctx.delivery.grants.revoke_user(chat_id, event.user_id)
        await ctx.delivery.send_reply(chat_id, SUBSCRIBED_TEXT, reply_to=reply_to, thread_id=thread_id)

    elif outcome.kind == "notify_off":
        _removed, grant = outcome.payload
        await ctx.delivery.confirm_notify_off(grant, UNSUBSCRIBED_TEXT)

    elif outcome.kind == "ping":
        snapshot, generation = outcome.payload
        await ctx.delivery.run_ping(
            chat_id, event.user_id, event.message_id, thread_id, generation, snapshot
        )

    elif outcome.kind == "list":
        subs = outcome.payload
        if not subs:
            await ctx.delivery.send_reply(chat_id, rendering.PONG, reply_to=reply_to, thread_id=thread_id)
        else:
            for chunk in rendering.subscriber_list_text(subs):
                await ctx.delivery.send_reply(chat_id, chunk, reply_to=reply_to, thread_id=thread_id)

    elif outcome.kind == "help":
        actor = outcome.payload
        text = rendering.help_text(actor, scope=Scope.GROUP, chat_active=True)
        await ctx.delivery.send_reply(chat_id, text, reply_to=reply_to, thread_id=thread_id)

    else:
        raise AssertionError(f"unhandled outcome kind {outcome.kind!r}")
