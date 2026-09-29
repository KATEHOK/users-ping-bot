"""Pure access policy: no I/O, no exceptions on denial."""

from dataclasses import dataclass

from .models import Actor, Cmd, Scope


@dataclass(frozen=True, slots=True)
class CommandSpec:
    cmd: Cmd
    scope: Scope
    syntax: str  # descriptions live in the rendering catalogue (cmd_* keys)
    alias: str = ""  # short group form shown next to the syntax (parsing: commands.py)


CATALOG: tuple[CommandSpec, ...] = (
    # --- group ---
    CommandSpec(Cmd.CHAT_REGISTER, Scope.GROUP, "/upb chat register"),
    CommandSpec(Cmd.CHAT_UNREGISTER, Scope.GROUP, "/upb chat unregister"),
    CommandSpec(Cmd.NOTIFY_ON, Scope.GROUP, "/upb notify on", "/on"),
    CommandSpec(Cmd.NOTIFY_OFF, Scope.GROUP, "/upb notify off", "/off"),
    CommandSpec(Cmd.PING, Scope.GROUP, "/upb all", "/all"),
    CommandSpec(Cmd.LIST, Scope.GROUP, "/upb list"),
    CommandSpec(Cmd.HELP, Scope.GROUP, "/upb help", "/help"),
    CommandSpec(Cmd.LANG, Scope.GROUP, "/upb lang <en|ru>"),
    # internal: help for a bare/partial/unknown /upb; never listed in help
    CommandSpec(Cmd.USAGE, Scope.GROUP, "/upb"),
    # --- private ---
    CommandSpec(Cmd.P_HELP, Scope.PRIVATE, "/help"),
    CommandSpec(Cmd.P_LANG, Scope.PRIVATE, "/lang <en|ru>"),
    CommandSpec(Cmd.ADMIN_CREATE, Scope.PRIVATE, "/admin create <user_id>"),
    CommandSpec(Cmd.ADMIN_REMOVE, Scope.PRIVATE, "/admin remove <user_id>"),
    CommandSpec(Cmd.ADMIN_LIST, Scope.PRIVATE, "/admin list"),
    CommandSpec(Cmd.CHAT_LIST, Scope.PRIVATE, "/chat list"),
    CommandSpec(Cmd.CHAT_REMOVE, Scope.PRIVATE, "/chat remove <chat_id>"),
    # internal: help for a bare/partial /admin, /chat, /lang; never listed in help
    CommandSpec(Cmd.P_USAGE, Scope.PRIVATE, "/admin, /chat"),
)

# Internal entries: reachable by typing a prefix, but never listed as commands.
INTERNAL: frozenset[Cmd] = frozenset({Cmd.USAGE, Cmd.P_USAGE})

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
            # Free chat: any admin or root may register it; nothing else is available.
            return cmd in (Cmd.CHAT_REGISTER, Cmd.USAGE) and actor.is_staff
        if cmd in (Cmd.CHAT_REGISTER, Cmd.CHAT_UNREGISTER, Cmd.LANG):
            return actor.is_chat_owner
        if cmd in (Cmd.NOTIFY_ON, Cmd.USAGE):
            return True
        if cmd in (Cmd.NOTIFY_OFF, Cmd.PING, Cmd.LIST, Cmd.HELP):
            return actor.is_subscriber or actor.is_chat_owner
        return False

    # PRIVATE: only root/admin, never a bare subscription or chat ownership
    if cmd in (Cmd.P_HELP, Cmd.P_LANG, Cmd.P_USAGE, Cmd.CHAT_LIST):
        return actor.is_staff
    if cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST, Cmd.CHAT_REMOVE):
        return actor.is_root
    return False


def allowed_commands(
    actor: Actor, *, scope: Scope, chat_active: bool
) -> tuple[CommandSpec, ...]:
    """Every runnable catalogue entry, internal ones included (help filters those out)."""
    return tuple(
        s
        for s in CATALOG
        if s.scope is scope and can_run(s.cmd, actor, scope=scope, chat_active=chat_active)
    )
