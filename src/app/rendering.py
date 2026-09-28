"""Texts, HTML escaping and message splitting. Pure: no I/O."""

import html
from collections.abc import Sequence
from typing import Protocol

from . import access
from .models import DEFAULT_LANG, Actor, Cmd, Lang, Scope, SubscriberRef

MAX_MESSAGE = 3900  # safety margin under Telegram's 4096
MAX_MENTIONS = 50  # per message; the real entity limit is unverified, keep a margin

PONG = "pong"

# key -> (en, ru). Placeholders are str.format fields; their values are HTML-escaped by t().
_CATALOG: dict[str, tuple[str, str]] = {
    "welcome": (
        "Chat registered. Subscribe: /upb notify on. Ping: /upb all. Help: /upb help.",
        "\u0427\u0430\u0442 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d. \u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430: /upb notify on. \u041f\u0438\u043d\u0433: /upb all. \u0421\u043f\u0440\u0430\u0432\u043a\u0430: /upb help.",
    ),
    "already_registered": ("Chat is already registered.", "\u0427\u0430\u0442 \u0443\u0436\u0435 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d."),
    "subscribed": ("Subscribed.", "\u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0430."),
    "already_subscribed": ("Already subscribed.", "\u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u0443\u0436\u0435 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0430."),
    "unsubscribed": ("Unsubscribed.", "\u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u0430."),
    "not_subscribed": ("Not subscribed.", "\u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0438 \u043d\u0435 \u0431\u044b\u043b\u043e."),
    "list_empty": ("No subscribers yet.", "\u041f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u043a\u043e\u0432 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442."),
    "pong": ("pong", "pong"),
    "farewell": ("Chat unregistered. Bye!", "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0447\u0430\u0442\u0430 \u0441\u043d\u044f\u0442\u0430. \u0414\u043e \u0432\u0441\u0442\u0440\u0435\u0447\u0438!"),
    "lang_set": ("Language: English.", "\u042f\u0437\u044b\u043a: \u0440\u0443\u0441\u0441\u043a\u0438\u0439."),
    "root_cli_only": ("Root is assigned via CLI only.", "Root \u043d\u0430\u0437\u043d\u0430\u0447\u0430\u0435\u0442\u0441\u044f \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0435\u0440\u0435\u0437 CLI."),
    "bad_args": ("Invalid arguments. Usage: {syntax}", "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0435 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u044b. \u0424\u043e\u0440\u043c\u0430\u0442: {syntax}"),
    "admin_created": ("Admin granted: {id}.", "\u0420\u043e\u043b\u044c admin \u0432\u044b\u0434\u0430\u043d\u0430: {id}."),
    "admin_exists": ("Already admin: {id}.", "\u0420\u043e\u043b\u044c admin \u0443\u0436\u0435 \u0435\u0441\u0442\u044c: {id}."),
    "admin_removed": (
        "Admin revoked: {id}. Chats removed: {n}.",
        "\u0420\u043e\u043b\u044c admin \u0441\u043d\u044f\u0442\u0430: {id}. \u0421\u043d\u044f\u0442\u043e \u0447\u0430\u0442\u043e\u0432: {n}.",
    ),
    "admin_absent": ("Not an admin: {id}.", "\u0420\u043e\u043b\u0438 admin \u043d\u0435\u0442: {id}."),
    "chat_removed": ("Chat unregistered: {chat_id}.", "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0441\u043d\u044f\u0442\u0430: {chat_id}."),
    "chat_absent": (
        "No active registration: {chat_id}.",
        "\u0410\u043a\u0442\u0438\u0432\u043d\u043e\u0439 \u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438 \u043d\u0435\u0442: {chat_id}.",
    ),
    "root_revoked": ("Root role revoked at {time}.", "\u0420\u043e\u043b\u044c root \u0441\u043d\u044f\u0442\u0430: {time}."),
    "startup": (
        "Bot started. Chats removed on check: {n}.",
        "\u0411\u043e\u0442 \u0437\u0430\u043f\u0443\u0449\u0435\u043d. \u0421\u043d\u044f\u0442\u043e \u0447\u0430\u0442\u043e\u0432 \u043f\u0440\u0438 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0435: {n}.",
    ),
    "name_unknown": ("name unknown", "\u0438\u043c\u044f \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e"),
    "ping_note": (
        "Recipients are fixed at the command. Delivery is not guaranteed.",
        "\u041f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0438 \u0444\u0438\u043a\u0441\u0438\u0440\u0443\u044e\u0442\u0441\u044f \u0432 \u043c\u043e\u043c\u0435\u043d\u0442 \u043a\u043e\u043c\u0430\u043d\u0434\u044b. \u0414\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u043d\u0435 \u0433\u0430\u0440\u0430\u043d\u0442\u0438\u0440\u0443\u0435\u0442\u0441\u044f.",
    ),
    # not in decisions section 10
    "help_title": ("Commands:", "\u041a\u043e\u043c\u0430\u043d\u0434\u044b:"),
    "help_register_hint": (
        "To register a group: add the bot there and send /upb chat register.",
        "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0433\u0440\u0443\u043f\u043f\u044b: \u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u0431\u043e\u0442\u0430 \u0432 \u0433\u0440\u0443\u043f\u043f\u0443 \u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 /upb chat register.",
    ),
    "chats_empty": ("No registered chats.", "\u0417\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u044b\u0445 \u0447\u0430\u0442\u043e\u0432 \u043d\u0435\u0442."),
    "admins_empty": ("No admins.", "\u0410\u0434\u043c\u0438\u043d\u043e\u0432 \u043d\u0435\u0442."),
    "startup_removed": ("Removed: {ids}.", "\u0421\u043d\u044f\u0442\u043e: {ids}."),
    # command summaries, one per listed command
    "cmd_chat_register": ("Register this chat.", "\u0417\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0447\u0430\u0442."),
    "cmd_chat_unregister": ("Unregister this chat.", "\u0421\u043d\u044f\u0442\u044c \u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044e \u0447\u0430\u0442\u0430."),
    "cmd_notify_on": ("Subscribe yourself.", "\u041f\u043e\u0434\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f."),
    "cmd_notify_off": ("Unsubscribe yourself.", "\u041e\u0442\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f."),
    "cmd_ping": ("Ping all subscribers.", "\u041f\u043e\u0437\u0432\u0430\u0442\u044c \u0432\u0441\u0435\u0445 \u043f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u043a\u043e\u0432."),
    "cmd_list": ("List subscribers.", "\u0421\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u043a\u043e\u0432."),
    "cmd_help": ("Show this help.", "\u042d\u0442\u0430 \u0441\u043f\u0440\u0430\u0432\u043a\u0430."),
    "cmd_lang": ("Set the chat language.", "\u042f\u0437\u044b\u043a \u0447\u0430\u0442\u0430."),
    "cmd_p_help": ("Show this help.", "\u042d\u0442\u0430 \u0441\u043f\u0440\u0430\u0432\u043a\u0430."),
    "cmd_p_lang": ("Set your language.", "\u042f\u0437\u044b\u043a \u0432 \u043b\u0438\u0447\u043a\u0435."),
    "cmd_admin_create": ("Grant the admin role.", "\u0412\u044b\u0434\u0430\u0442\u044c \u0440\u043e\u043b\u044c admin."),
    "cmd_admin_remove": ("Revoke the admin role.", "\u0421\u043d\u044f\u0442\u044c \u0440\u043e\u043b\u044c admin."),
    "cmd_admin_list": ("List admins.", "\u0421\u043f\u0438\u0441\u043e\u043a \u0430\u0434\u043c\u0438\u043d\u043e\u0432."),
    "cmd_chat_list": ("List registered chats.", "\u0421\u043f\u0438\u0441\u043e\u043a \u0447\u0430\u0442\u043e\u0432."),
    "cmd_chat_remove": ("Unregister a chat.", "\u0421\u043d\u044f\u0442\u044c \u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044e \u0447\u0430\u0442\u0430."),
}

CATALOG_KEYS: tuple[str, ...] = tuple(_CATALOG)


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def t(key: str, lang: Lang, **kw: object) -> str:
    """Catalogue text; every keyword value is HTML-escaped."""
    pair = _CATALOG[key]
    template = pair[1] if lang == "ru" else pair[0]
    return template.format(**{k: esc(str(v)) for k, v in kw.items()})


def mention(user_id: int, display_name: str | None, username: str | None) -> str:
    # Always an id-anchored link, never an @username ping: usernames are cached
    # hints for lists only, not stable delivery addresses.
    del username  # kept for signature parity with the contract; not used for the label
    label = display_name if display_name else f"id{user_id}"
    return f'<a href="tg://user?id={user_id}">{esc(label)}</a>'


def split_mentions(
    parts: Sequence[str], *, limit: int = MAX_MESSAGE, max_items: int = MAX_MENTIONS
) -> list[str]:
    """Join mentions with spaces into chunks of <= max_items and <= limit chars, never splitting one."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in parts:
        extra = len(part) + (1 if current else 0)
        if current and (len(current) >= max_items or current_len + extra > limit):
            chunks.append(" ".join(current))
            current = []
            current_len = 0
            extra = len(part)
        current.append(part)
        current_len += extra
    if current:
        chunks.append(" ".join(current))
    return chunks


def split_text(text: str, *, limit: int = MAX_MESSAGE) -> list[str]:
    """Splits on newline boundaries into chunks under limit."""
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and current_len + extra > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
            extra = len(line)
        current.append(line)
        current_len += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def _command_lines(specs: Sequence[access.CommandSpec], lang: Lang) -> list[str]:
    return [f"{esc(s.syntax)} - {t('cmd_' + s.cmd.value, lang)}" for s in specs]


def _listed(specs: Sequence[access.CommandSpec]) -> list[access.CommandSpec]:
    return [s for s in specs if s.cmd not in access.INTERNAL]


def help_text(actor: Actor, *, scope: Scope, chat_active: bool, lang: Lang = DEFAULT_LANG) -> str:
    specs = _listed(access.allowed_commands(actor, scope=scope, chat_active=chat_active))
    lines = [t("help_title", lang), *_command_lines(specs, lang)]
    if scope is Scope.PRIVATE:
        lines.append(t("help_register_hint", lang))
    if any(s.cmd is Cmd.PING for s in specs):
        lines.append(t("ping_note", lang))
    return "\n".join(lines)


def _words(syntax: str) -> list[str]:
    """Words after the command prefix: '/upb chat register' -> [chat, register]."""
    parts = syntax.split()
    return parts[1:] if parts[0] == "/upb" else [parts[0].lstrip("/"), *parts[1:]]


_KNOWN_PREFIXES: dict[Scope, frozenset[str]] = {
    Scope.GROUP: frozenset({"chat", "notify", "lang"}),
    Scope.PRIVATE: frozenset({"admin", "chat", "lang"}),
}


def usage_text(
    actor: Actor,
    *,
    scope: Scope,
    chat_active: bool,
    prefix: tuple[str, ...],
    lang: Lang = DEFAULT_LANG,
) -> str:
    """Help for a typed prefix. Returns "" when there is nothing to show: callers stay silent.

    Known prefix: only allowed commands under it (no fallback). Bare or unknown: all allowed.
    """
    specs = _listed(access.allowed_commands(actor, scope=scope, chat_active=chat_active))
    want = [p.lower() for p in prefix]
    if want and want[0] in _KNOWN_PREFIXES[scope]:
        specs = [s for s in specs if _words(s.syntax)[: len(want)] == want]
    if not specs:
        return ""
    return "\n".join([t("help_title", lang), *_command_lines(specs, lang)])


def welcome_text(lang: Lang = DEFAULT_LANG) -> str:
    return t("welcome", lang)


def farewell_text(lang: Lang = DEFAULT_LANG) -> str:
    return t("farewell", lang)


def root_revoked_text(when: str, lang: Lang = DEFAULT_LANG) -> str:
    return t("root_revoked", lang, time=when)


def _safe(text: str) -> str:
    # Telegram auto-links @name in plain text: swap in the fullwidth sign to keep lists inert.
    return esc(text.replace("@", "\uff20"))


def _plain_label(display_name: str | None, username: str | None, lang: Lang) -> str:
    name = _safe(display_name) if display_name else t("name_unknown", lang)
    return f"{name} ({_safe(username.lstrip('@'))})" if username else name


def subscriber_list_text(subs: Sequence[SubscriberRef], lang: Lang = DEFAULT_LANG) -> list[str]:
    # Empty input yields no chunks; callers reply with list_empty.
    if not subs:
        return []
    lines = [f"{_plain_label(s.display_name, s.username, lang)} - {s.user_id}" for s in subs]
    return split_text("\n".join(lines))


class ChatRowLike(Protocol):
    chat_id: int
    title: str | None
    registered_by: int


class AdminRowLike(Protocol):
    user_id: int
    display_name: str | None
    username: str | None


def _chat_lines(rows: Sequence[ChatRowLike], lang: Lang, show_registrar: bool) -> list[str]:
    lines = []
    for r in rows:
        title = _safe(r.title) if r.title else t("name_unknown", lang)
        line = f"{title} | {r.chat_id}"
        if show_registrar:
            line += f" | {r.registered_by}"
        lines.append(line)
    return lines


def chat_list_text(
    rows: Sequence[ChatRowLike], lang: Lang = DEFAULT_LANG, *, show_registrar: bool
) -> list[str]:
    if not rows:
        return []
    return split_text("\n".join(_chat_lines(rows, lang, show_registrar)))


def admin_list_text(rows: Sequence[AdminRowLike], lang: Lang = DEFAULT_LANG) -> list[str]:
    if not rows:
        return []
    lines = [f"{_plain_label(r.display_name, r.username, lang)} - {r.user_id}" for r in rows]
    return split_text("\n".join(lines))


def startup_report_text(
    removed_chat_ids: Sequence[int],
    rows: Sequence[ChatRowLike],
    lang: Lang = DEFAULT_LANG,
) -> list[str]:
    lines = [t("startup", lang, n=len(removed_chat_ids))]
    if removed_chat_ids:
        lines.append(t("startup_removed", lang, ids=", ".join(str(i) for i in removed_chat_ids)))
    lines += _chat_lines(rows, lang, True)
    return split_text("\n".join(lines))
