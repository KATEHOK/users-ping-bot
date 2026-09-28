"""Pure access policy: no I/O, no exceptions on denial."""

from dataclasses import dataclass

from .models import Actor, Cmd, Scope


@dataclass(frozen=True, slots=True)
class CommandSpec:
    cmd: Cmd
    scope: Scope
    syntax: str
    summary: str


CATALOG: tuple[CommandSpec, ...] = (
    # --- group ---
    CommandSpec(
        Cmd.CHAT_REGISTER,
        Scope.GROUP,
        "/upb chat register",
        "Register this chat for ping notifications.",
    ),
    CommandSpec(
        Cmd.CHAT_UNREGISTER,
        Scope.GROUP,
        "/upb chat unregister",
        "Unregister this chat and clear its subscribers.",
    ),
    CommandSpec(
        Cmd.NOTIFY_ON,
        Scope.GROUP,
        "/upb notify on",
        "Subscribe yourself to pings in this chat.",
    ),
    CommandSpec(
        Cmd.NOTIFY_OFF,
        Scope.GROUP,
        "/upb notify off",
        "Unsubscribe yourself from pings in this chat.",
    ),
    CommandSpec(
        Cmd.PING,
        Scope.GROUP,
        "/upb all",
        "Ping every subscriber of this chat (alias: /upb notify all).",
    ),
    CommandSpec(
        Cmd.LIST,
        Scope.GROUP,
        "/upb list",
        "List subscribers of this chat.",
    ),
    CommandSpec(
        Cmd.HELP,
        Scope.GROUP,
        "/upb help",
        "Show the commands available to you here (alias: /upb usage).",
    ),
    # iter2 placeholders: syntax and rules are defined by the presentation rework
    CommandSpec(Cmd.LANG, Scope.GROUP, "/upb lang <en|ru>", "Set the reply language for this chat."),
    CommandSpec(Cmd.USAGE, Scope.GROUP, "/upb", "Show help for the typed command prefix."),
    # --- private ---
    CommandSpec(
        Cmd.P_HELP,
        Scope.PRIVATE,
        "/help",
        "Show the commands available to you in a private chat (aliases: /usage, /start).",
    ),
    CommandSpec(
        Cmd.ADMIN_CREATE,
        Scope.PRIVATE,
        "/admin create <user_id>",
        "Grant the admin role to a user.",
    ),
    CommandSpec(
        Cmd.ADMIN_REMOVE,
        Scope.PRIVATE,
        "/admin remove <user_id>",
        "Revoke the admin role and drop all of that admin's chats.",
    ),
    CommandSpec(
        Cmd.ADMIN_LIST,
        Scope.PRIVATE,
        "/admin list",
        "List all users with the admin role.",
    ),
    CommandSpec(
        Cmd.CHAT_LIST,
        Scope.PRIVATE,
        "/chat list",
        "List all registered chats.",
    ),
    CommandSpec(
        Cmd.CHAT_REMOVE,
        Scope.PRIVATE,
        "/chat remove <chat_id>",
        "Remove a chat's registration and drop its subscriptions.",
    ),
)

CATALOG += (
    CommandSpec(Cmd.P_LANG, Scope.PRIVATE, "/lang <en|ru>", "Set your reply language."),
    CommandSpec(Cmd.P_USAGE, Scope.PRIVATE, "/admin, /chat", "Show help for the typed command prefix."),
)

_BY_CMD: dict[Cmd, CommandSpec] = {s.cmd: s for s in CATALOG}


def spec(cmd: Cmd) -> CommandSpec:
    return _BY_CMD[cmd]


def can_run(cmd: Cmd, actor: Actor, *, scope: Scope, chat_active: bool) -> bool:
    """Silent policy check: never raises, never explains. False just means no reply."""
    s = _BY_CMD.get(cmd)
    if s is None or s.scope is not scope:
        return False

    if scope is Scope.GROUP:
        if not chat_active:
            # Unregistered group: only staff may register it. Nothing else, not even for root.
            return cmd is Cmd.CHAT_REGISTER and actor.is_staff
        if cmd in (Cmd.CHAT_REGISTER, Cmd.CHAT_UNREGISTER):
            return actor.is_staff
        if cmd is Cmd.NOTIFY_ON:
            return True  # explicit self-join exception: anybody may subscribe themselves
        if cmd is Cmd.NOTIFY_OFF:
            return actor.is_subscriber or actor.is_staff
        if cmd in (Cmd.PING, Cmd.LIST, Cmd.HELP):
            return actor.is_subscriber or actor.is_staff
        return False

    # scope is PRIVATE: answers only root/admin, never a bare subscription
    if cmd in (Cmd.P_HELP, Cmd.CHAT_LIST):
        return actor.is_staff
    if cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST, Cmd.CHAT_REMOVE):
        return actor.is_root
    return False


def allowed_commands(
    actor: Actor, *, scope: Scope, chat_active: bool
) -> tuple[CommandSpec, ...]:
    """What help generation consumes; keeps help from drifting off the policy above."""
    return tuple(
        s
        for s in CATALOG
        if s.scope is scope and can_run(s.cmd, actor, scope=scope, chat_active=chat_active)
    )
