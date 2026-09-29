"""Private-chat command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import access, commands, rendering
from ..models import Cmd, IncomingEvent, Role, Scope
from ..rendering import t

if TYPE_CHECKING:
    from . import Context


async def handle(ctx: Context, event: IncomingEvent) -> None:
    from . import mark_ignored

    assert event.user_id is not None
    parsed = commands.parse_private_command(event.text, event.entities, bot_username=ctx.bot_username)
    replies: list[str] = []
    menu: list[int] = []  # chats unregistered by this command

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return  # duplicate

        # every private message of an identified user records the contact
        await ctx.services.touch_user(
            c,
            event.user_id,
            username=event.username,
            display_name=event.display_name,
            private_contact=True,
        )

        if parsed is None:
            await mark_ignored(c, ctx, event.update_id)
            return

        cmd = parsed.cmd
        actor = await ctx.services.load_actor(c, event.user_id)
        if not access.can_run(cmd, actor, scope=Scope.PRIVATE, chat_active=True):
            await mark_ignored(c, ctx, event.update_id)
            return

        lang = await ctx.services.get_user_lang(c, event.user_id)
        try:
            arg = commands.validate_args(cmd, parsed.args)
        except ValueError:
            replies.append(t("bad_args", lang, syntax=access.spec(cmd).syntax))
        else:
            replies = await _execute(ctx, c, cmd, arg, parsed.args, actor, lang, menu)

        if not replies:
            await mark_ignored(c, ctx, event.update_id)

    for text in replies:
        if not await ctx.delivery.send_reply(
            event.chat_id, text, reply_to=event.message_id, thread_id=event.thread_id
        ):
            break
    await ctx.delivery.sync_chat_menus(menu, single_attempt=True)


async def _execute(ctx, c, cmd: Cmd, arg, args, actor, lang, menu: list[int]) -> list[str]:
    svc = ctx.services

    if cmd is Cmd.P_HELP:
        return [rendering.help_text(actor, scope=Scope.PRIVATE, chat_active=True, lang=lang)]

    if cmd is Cmd.P_LANG:
        await svc.set_user_lang(c, actor.user_id, arg)
        return [t("lang_set", arg)]

    if cmd is Cmd.ADMIN_CREATE:
        result = await svc.grant_admin(c, arg)
        if result.status == "is_root":
            return [t("root_cli_only", lang)]
        return [t("admin_created" if result.status == "created" else "admin_exists", lang, id=arg)]

    if cmd is Cmd.ADMIN_REMOVE:
        if await svc.get_role(c, arg) is Role.ROOT:
            return [t("root_cli_only", lang)]  # root changes only through the CLI
        result = await svc.revoke_admin(c, arg)
        if not result.revoked:
            return [t("admin_absent", lang, id=arg)]
        menu.extend(result.chat_ids)
        return [t("admin_removed", lang, id=arg, n=len(result.chat_ids))]

    if cmd is Cmd.ADMIN_LIST:
        return rendering.admin_list_text(await svc.list_admins(c), lang) or [t("admins_empty", lang)]

    if cmd is Cmd.CHAT_LIST:
        if actor.is_root:
            rows = await svc.list_chats(c)
        else:
            rows = await svc.list_chats(c, registered_by=actor.user_id)
        return rendering.chat_list_text(rows, lang, show_registrar=actor.is_root) or [
            t("chats_empty", lang)
        ]

    if cmd is Cmd.CHAT_REMOVE:
        result = await svc.remove_chat(c, arg)  # resolves an aliased (migrated) id itself
        if result.chat_ids:
            menu.extend(result.chat_ids)
            return [t("chat_removed", lang, chat_id=result.chat_ids[0])]
        return [t("chat_absent", lang, chat_id=arg)]

    if cmd is Cmd.P_USAGE:
        text = rendering.usage_text(
            actor, scope=Scope.PRIVATE, chat_active=True, prefix=args, lang=lang
        )
        return [text] if text else []

    raise AssertionError(f"unhandled private command {cmd!r}")
