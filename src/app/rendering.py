"""Texts, HTML escaping and message splitting. Pure: no I/O."""

import html
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from . import access
from .models import Actor, Cmd, Scope, SubscriberRef

MAX_MESSAGE = 3900  # safety margin under Telegram's 4096

PONG = "pong"


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def mention(user_id: int, display_name: str | None, username: str | None) -> str:
    # Always an id-anchored link, never an @username ping: usernames are cached
    # hints for lists only, not stable delivery addresses (plan section 10).
    del username  # kept for signature parity with the contract; not used for the label
    label = display_name if display_name else f"id{user_id}"
    return f'<a href="tg://user?id={user_id}">{esc(label)}</a>'


def split_mentions(parts: Sequence[str], *, limit: int = MAX_MESSAGE) -> list[str]:
    """Join mention strings with spaces into chunks under limit; never splits one mention."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in parts:
        extra = len(part) + (1 if current else 0)
        if current and current_len + extra > limit:
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


_PING_DELIVERY_NOTE = (
    "Recipients are fixed the moment this command is accepted: later unsubscribes are "
    "excluded and new subscribers only land in the next ping. Delivery is not guaranteed."
)


def help_text(actor: Actor, *, scope: Scope, chat_active: bool) -> str:
    specs = access.allowed_commands(actor, scope=scope, chat_active=chat_active)
    lines = ["Available commands:"]
    lines += [f"{s.syntax} - {s.summary}" for s in specs]

    if scope is Scope.GROUP and actor.is_staff:
        lines.append("Admin/root role management and the chat list live in a private chat with the bot.")
    if scope is Scope.PRIVATE and actor.is_root:
        lines.append("Manage admin roles here, in this private chat, via /admin create|remove|list.")
    if any(s.cmd is Cmd.PING for s in specs):
        lines.append(_PING_DELIVERY_NOTE)

    return "\n".join(lines)


def welcome_text() -> str:
    return "\n".join(
        [
            "This chat is now registered for ping notifications.",
            "Use /upb notify on to subscribe yourself.",
            "Subscribers can run /upb all to ping everyone, /upb list to see subscribers, "
            "and /upb help for the full list.",
        ]
    )


def farewell_text() -> str:
    return "This chat has been unregistered. Ping notifications are disabled here."


def root_revoked_text(when: str) -> str:
    return f"Your root role was revoked at {when}."


def subscriber_list_text(subs: Sequence[SubscriberRef]) -> list[str]:
    # Empty input yields no chunks; callers reply PONG for an empty list/ping (plan section 4).
    if not subs:
        return []
    lines = [_plain_label(s.display_name, s.username) + f" - {s.user_id}" for s in subs]
    return split_text("\n".join(lines))


class ChatRowLike(Protocol):
    chat_id: int
    title: str | None
    registered_by: int
    blocked: bool


class AdminRowLike(Protocol):
    user_id: int
    display_name: str | None
    username: str | None


@dataclass(frozen=True, slots=True)
class ChatListRow:
    """Convenience concrete row matching ChatRowLike; services.py may pass its own instead."""

    chat_id: int
    title: str | None
    registered_by: int
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class AdminListRow:
    """Convenience concrete row matching AdminRowLike; services.py may pass its own instead."""

    user_id: int
    display_name: str | None
    username: str | None


def chat_list_text(rows: Sequence[ChatRowLike]) -> list[str]:
    if not rows:
        return []
    lines = []
    for r in rows:
        title = esc(r.title) if r.title else "name unknown"
        line = f"{title} | chat_id={r.chat_id} | registrar={r.registered_by}"
        if r.blocked:
            line += " | BLOCKED (migration conflict)"
        lines.append(line)
    return split_text("\n".join(lines))


def admin_list_text(rows: Sequence[AdminRowLike]) -> list[str]:
    if not rows:
        return []
    lines = [_plain_label(r.display_name, r.username) + f" - {r.user_id}" for r in rows]
    return split_text("\n".join(lines))


def _plain_label(display_name: str | None, username: str | None) -> str:
    if display_name:
        return esc(display_name)
    if username:
        return esc(f"@{username}")
    return "name unknown"
