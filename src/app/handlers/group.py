"""Group command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import access, commands, rendering
from ..models import DEFAULT_LANG, Cmd, IncomingEvent, Scope, SubscriberRef
from ..rendering import t

if TYPE_CHECKING:
    from . import Context

_COOLDOWN_GC_SIZE = 1000


def _now(ctx: Context) -> float:
    return ctx.clock.now().timestamp()


def _ping_limited(ctx: Context, chat_id: int, user_id: int, is_root: bool) -> bool:
    if is_root or ctx.ping_cooldown_seconds <= 0:
        return False
    last = ctx.ping_last.get((chat_id, user_id))
    if last is None:
        return False
    # a negative difference means the wall clock went back: the interval has expired
    return 0 <= _now(ctx) - last < ctx.ping_cooldown_seconds


def _note_ping(ctx: Context, chat_id: int, user_id: int) -> None:
    now = _now(ctx)
    if len(ctx.ping_last) >= _COOLDOWN_GC_SIZE:
        cutoff = now - ctx.ping_cooldown_seconds
        for key in [k for k, v in ctx.ping_last.items() if v <= cutoff or v > now]:
            del ctx.ping_last[key]
    ctx.ping_last[(chat_id, user_id)] = now


def _syntax(cmd: Cmd, via_alias: bool) -> str:
    s = access.spec(cmd)
    return (s.alias or s.syntax) if via_alias else s.syntax


async def handle(ctx: Context, event: IncomingEvent) -> None:
    from . import mark_ignored

    parsed = commands.parse_group_command(event.text, event.entities, bot_username=ctx.bot_username)
    if parsed is None:
        return  # not a /upb command or alias: never recorded

    cmd = parsed.cmd
    assert event.user_id is not None
    replies: list[str] = []
    ping: list[SubscriberRef] | None = None
    ping_key: tuple[int, int] | None = None
    menu: list[int] = []  # chats whose command menu may have changed
    menu_users: list[int] = []  # users whose member menu may be stale

    async with ctx.db.transaction() as c:
        if not await ctx.services.claim_update(c, ctx.bot_id, event.update_id):
            return  # duplicate

        await ctx.services.touch_user(
            c, event.user_id, username=event.username, display_name=event.display_name
        )

        chat_id = await ctx.services.resolve_chat_id(c, event.chat_id)
        if chat_id != event.chat_id:
            await mark_ignored(c, ctx, event.update_id)  # migrated old group
            return

        chat = await ctx.services.get_chat(c, chat_id)
        active = chat is not None
        lang = chat.lang if chat is not None else DEFAULT_LANG
        actor = await ctx.services.load_actor(c, event.user_id, chat_id=chat_id)

        # the only authorization check of this reaction
        if not access.can_run(cmd, actor, scope=Scope.GROUP, chat_active=active):
            await mark_ignored(c, ctx, event.update_id)
            return

        new_lang = lang
        if cmd is Cmd.LANG:
            try:
                new_lang = commands.validate_args(cmd, parsed.args)  # type: ignore[assignment]
            except ValueError:
                replies.append(t("bad_args", lang, syntax=_syntax(cmd, parsed.via_alias)))
                cmd = Cmd.USAGE  # nothing more to do below
        elif cmd is Cmd.PING and _ping_limited(ctx, chat_id, event.user_id, actor.is_root):
            await mark_ignored(c, ctx, event.update_id)
            return

        if cmd is Cmd.CHAT_REGISTER:
            result = await ctx.services.register_chat(c, chat_id, event.chat_title, event.user_id)
            replies.append(rendering.welcome_text(lang) if result.created else t("already_registered", lang))
            menu.append(chat_id)
            menu_users.extend(await ctx.services.staff_ids(c))  # stale register menus
        elif cmd is Cmd.CHAT_UNREGISTER:
            result = await ctx.services.unregister_chat(c, chat_id)  # farewell goes via the outbox
            menu.append(chat_id)
            menu_users.extend(result.owner_ids)
        elif cmd is Cmd.NOTIFY_ON:
            sub = await ctx.services.subscribe(c, chat_id, event.user_id)
            replies.append(t("subscribed" if sub.created else "already_subscribed", lang))
        elif cmd is Cmd.NOTIFY_OFF:
            removed = await ctx.services.unsubscribe(c, chat_id, event.user_id)
            replies.append(t("unsubscribed" if removed else "not_subscribed", lang))
        elif cmd is Cmd.PING:
            ping = await ctx.services.list_subscribers(c, chat_id, exclude_user_id=event.user_id)
            ping_key = (chat_id, event.user_id)
        elif cmd is Cmd.LIST:
            subs = await ctx.services.list_subscribers(c, chat_id)
            replies.extend(rendering.subscriber_list_text(subs, lang) or [t("list_empty", lang)])
        elif cmd is Cmd.HELP:
            replies.append(rendering.help_text(actor, scope=Scope.GROUP, chat_active=active, lang=lang))
        elif cmd is Cmd.LANG:
            await ctx.services.set_chat_lang(c, chat_id, new_lang)
            replies.append(t("lang_set", new_lang))
            menu.append(chat_id)
        elif cmd is Cmd.USAGE:
            if not replies:  # empty text means nothing to show: silence
                text = rendering.usage_text(
                    actor, scope=Scope.GROUP, chat_active=active, prefix=parsed.args, lang=lang
                )
                if text:
                    replies.append(text)
                else:
                    await mark_ignored(c, ctx, event.update_id)
        else:
            raise AssertionError(f"unhandled group command {cmd!r}")

    # after commit: what is sent is final
    if ping_key is not None:
        _note_ping(ctx, *ping_key)
    send = ctx.delivery
    if ping is not None:
        await send.run_ping(event.chat_id, event.message_id, event.thread_id, ping)
    for text in replies:
        if not await send.send_reply(
            event.chat_id, text, reply_to=event.message_id, thread_id=event.thread_id
        ):
            break
    await send.sync_chat_menus(menu, single_attempt=True, users=menu_users)
