"""Private-chat command handlers (plan section 5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

from .. import access, commands, rendering
from ..models import Actor, Cmd, IncomingEvent, Role, Scope

if TYPE_CHECKING:
    from . import Context

NO_ADMINS_TEXT = "No admins."
NO_CHATS_TEXT = "No registered chats."


@dataclass(slots=True)
class _Outcome:
    kind: str
    payload: Any = None


async def handle(ctx: Context, event: IncomingEvent) -> None:
    parsed = commands.parse_private_command(event.text, event.entities, bot_username=ctx.bot_username)
    cmd = parsed.cmd if parsed is not None else None
    outcome: _Outcome | None = None

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return  # duplicate: complete no-op

        # every private message of an identified user gets its contact recorded,
        # including plain text and commands the author has no right to (plan section 8)
        await ctx.services.touch_user(
            c,
            event.user_id,
            username=event.username,
            display_name=event.display_name,
            private_contact=True,
        )

        if cmd is None:
            return  # unknown command / plain text: silence, contact already recorded

        actor = await ctx.services.load_actor(c, event.user_id)
        if not access.can_run(cmd, actor, scope=Scope.PRIVATE, chat_active=True):
            return  # silent path: still committed the claim + contact touch above

        try:
            arg_value = commands.validate_args(cmd, parsed.args)
        except ValueError:
            outcome = _Outcome("syntax_error", cmd)
        else:
            outcome = await _execute(ctx, c, cmd, arg_value, actor)

    if outcome is not None:
        await _deliver(ctx, event, outcome)


async def _execute(
    ctx: Context, c: aiosqlite.Connection, cmd: Cmd, arg_value: int | None, actor: Actor
) -> _Outcome:
    if cmd is Cmd.P_HELP:
        return _Outcome("help", actor)

    if cmd is Cmd.ADMIN_CREATE:
        assert arg_value is not None
        result = await ctx.services.grant_admin(c, arg_value)
        return _Outcome("admin_create", (arg_value, result))

    if cmd is Cmd.ADMIN_REMOVE:
        assert arg_value is not None
        role = await ctx.services.get_role(c, arg_value)
        if role is Role.ROOT:
            # /admin remove <root_id> never touches root: only the CLI does
            return _Outcome("admin_remove_is_root", arg_value)
        result = await ctx.services.revoke_admin(c, arg_value)
        return _Outcome("admin_remove", (arg_value, result))

    if cmd is Cmd.ADMIN_LIST:
        rows = await ctx.services.list_admins(c)
        return _Outcome("admin_list", rows)

    if cmd is Cmd.CHAT_LIST:
        rows = await ctx.services.list_chats(c)
        return _Outcome("chat_list", rows)

    if cmd is Cmd.CHAT_REMOVE:
        assert arg_value is not None
        canonical = await ctx.services.resolve_chat_id(c, arg_value)
        chat = await ctx.services.get_chat(c, canonical)
        if chat is None:
            return _Outcome("chat_remove_noop", arg_value)
        result = await ctx.services.remove_chat_cascade(c, canonical)
        return _Outcome("chat_remove", result)

    raise AssertionError(f"unhandled private command {cmd!r}")


async def _deliver(ctx: Context, event: IncomingEvent, outcome: _Outcome) -> None:
    chat_id = event.chat_id
    reply_to = event.message_id

    async def reply(text: str) -> None:
        await ctx.delivery.send_reply(chat_id, text, reply_to=reply_to, thread_id=None)

    if outcome.kind == "syntax_error":
        cmd: Cmd = outcome.payload
        await reply(f"Invalid arguments. Usage: {access.spec(cmd).syntax}")

    elif outcome.kind == "help":
        actor: Actor = outcome.payload
        await reply(rendering.help_text(actor, scope=Scope.PRIVATE, chat_active=True))

    elif outcome.kind == "admin_create":
        user_id, result = outcome.payload
        if result.status == "created":
            await reply(f"Granted admin to user {user_id}.")
        elif result.status == "exists":
            await reply(f"User {user_id} is already an admin.")
        else:  # "is_root"
            await reply("That user is root; root is assigned only via the CLI.")

    elif outcome.kind == "admin_remove_is_root":
        user_id = outcome.payload
        await reply(f"User {user_id} is root; root is changed only via the CLI.")

    elif outcome.kind == "admin_remove":
        user_id, result = outcome.payload
        if result.revoked:
            for cid in result.chat_ids:
                ctx.delivery.cancel_chat(cid)
            await reply(
                f"Revoked admin from user {user_id} and removed {len(result.chat_ids)} chat(s)."
            )
        else:
            await reply(f"User {user_id} is not an admin.")

    elif outcome.kind == "admin_list":
        rows = outcome.payload
        for chunk in rendering.admin_list_text(rows) or [NO_ADMINS_TEXT]:
            await reply(chunk)

    elif outcome.kind == "chat_list":
        rows = outcome.payload
        for chunk in rendering.chat_list_text(rows) or [NO_CHATS_TEXT]:
            await reply(chunk)

    elif outcome.kind == "chat_remove_noop":
        chat_id_arg = outcome.payload
        await reply(f"No active registration found for chat {chat_id_arg}.")

    elif outcome.kind == "chat_remove":
        result = outcome.payload
        for cid in result.chat_ids:
            ctx.delivery.cancel_chat(cid)
        if result.admin_demoted is not None:
            await reply(
                f"Removed {len(result.chat_ids)} chat(s) and revoked admin "
                f"from user {result.admin_demoted}."
            )
        else:
            await reply(f"Removed {len(result.chat_ids)} chat(s).")

    else:
        raise AssertionError(f"unhandled outcome kind {outcome.kind!r}")
