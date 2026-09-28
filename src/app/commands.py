"""Pure command parsing: no I/O. Recognition only from a bot_command entity at offset 0."""

import re
from collections.abc import Sequence

from .models import LANGS, Cmd, Lang, ParsedCommand


def _leading_command(
    text: str | None,
    entities: tuple[tuple[str, int, int], ...],
    bot_username: str,
) -> tuple[str, str] | None:
    """Return (lowercase command name without slash/@bot, remainder text) or None."""
    if not text:
        return None
    entity = next(
        (e for e in entities if e[0] == "bot_command" and e[1] == 0 and e[2] > 0),
        None,
    )
    if entity is None:
        return None
    _, offset, length = entity
    token = text[offset : offset + length]
    if not token.startswith("/"):
        return None
    body = token[1:]
    name, sep, addressed = body.partition("@")
    if sep and addressed.lower() != bot_username.lower():
        return None  # addressed to a different bot
    return name.lower(), text[length:]


def parse_group_command(
    text: str | None,
    entities: tuple[tuple[str, int, int], ...],
    *,
    bot_username: str,
) -> ParsedCommand | None:
    leading = _leading_command(text, entities, bot_username)
    if leading is None:
        return None
    name, rest = leading
    if name != "upb":
        return None

    raw = text or ""
    words = rest.split()
    w = [x.lower() for x in words]

    def usage() -> ParsedCommand:
        return ParsedCommand(Cmd.USAGE, tuple(w[:1]), raw)

    if not w:
        return usage()
    if w[0] == "chat":
        if len(w) >= 2 and w[1] == "register":
            return ParsedCommand(Cmd.CHAT_REGISTER, tuple(words[2:]), raw)
        if len(w) >= 2 and w[1] == "unregister":
            return ParsedCommand(Cmd.CHAT_UNREGISTER, tuple(words[2:]), raw)
        return usage()
    if w[0] == "notify":
        if len(w) >= 2 and w[1] == "on":
            return ParsedCommand(Cmd.NOTIFY_ON, tuple(words[2:]), raw)
        if len(w) >= 2 and w[1] == "off":
            return ParsedCommand(Cmd.NOTIFY_OFF, tuple(words[2:]), raw)
        if len(w) >= 2 and w[1] == "all":
            return ParsedCommand(Cmd.PING, tuple(words[2:]), raw)
        return usage()
    if w[0] == "lang":
        if len(words) == 1:
            return usage()
        return ParsedCommand(Cmd.LANG, tuple(words[1:]), raw)
    if w[0] == "all":
        return ParsedCommand(Cmd.PING, tuple(words[1:]), raw)
    if w[0] == "list":
        return ParsedCommand(Cmd.LIST, tuple(words[1:]), raw)
    if w[0] in ("help", "usage"):
        return ParsedCommand(Cmd.HELP, tuple(words[1:]), raw)
    return usage()


def parse_private_command(
    text: str | None,
    entities: tuple[tuple[str, int, int], ...],
    *,
    bot_username: str,
) -> ParsedCommand | None:
    leading = _leading_command(text, entities, bot_username)
    if leading is None:
        return None
    name, rest = leading
    raw = text or ""
    words = rest.split()
    sub = words[0].lower() if words else ""

    if name in ("help", "usage", "start"):
        return ParsedCommand(Cmd.P_HELP, tuple(words), raw)

    if name == "lang":
        if not words:
            return ParsedCommand(Cmd.P_USAGE, ("lang",), raw)
        return ParsedCommand(Cmd.P_LANG, tuple(words), raw)

    if name == "admin":
        if sub == "create":
            return ParsedCommand(Cmd.ADMIN_CREATE, tuple(words[1:]), raw)
        if sub == "remove":
            return ParsedCommand(Cmd.ADMIN_REMOVE, tuple(words[1:]), raw)
        if sub == "list":
            return ParsedCommand(Cmd.ADMIN_LIST, tuple(words[1:]), raw)
        return ParsedCommand(Cmd.P_USAGE, ("admin",), raw)

    if name == "chat":
        if sub == "list":
            return ParsedCommand(Cmd.CHAT_LIST, tuple(words[1:]), raw)
        if sub == "remove":
            return ParsedCommand(Cmd.CHAT_REMOVE, tuple(words[1:]), raw)
        return ParsedCommand(Cmd.P_USAGE, ("chat",), raw)

    return None


_INT_RE = re.compile(r"-?[0-9]+")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _parse_id(value: str, label: str) -> int:
    # ASCII digits only ([0-9], not \d): rejects "1_000", " 1", Arabic-Indic digits, etc.
    if len(value) > 25 or not _INT_RE.fullmatch(value):
        raise ValueError(f"{label} must be an integer")
    n = int(value)
    if not _INT64_MIN <= n <= _INT64_MAX:
        raise ValueError(f"{label} is out of range")
    return n


def validate_args(cmd: Cmd, args: Sequence[str]) -> int | Lang | None:
    """Parsed id (ADMIN_*, CHAT_REMOVE) or lang code (LANG, P_LANG); ValueError on bad args."""
    if cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE):
        if len(args) != 1:
            raise ValueError("expected exactly one user_id argument")
        value = _parse_id(args[0], "user_id")
        if value <= 0:
            raise ValueError("user_id must be positive")
        return value

    if cmd is Cmd.CHAT_REMOVE:
        if len(args) != 1:
            raise ValueError("expected exactly one chat_id argument")
        return _parse_id(args[0], "chat_id")

    if cmd in (Cmd.LANG, Cmd.P_LANG):
        if len(args) != 1:
            raise ValueError("expected exactly one language argument")
        code = args[0].lower()
        for lang in LANGS:
            if code == lang:
                return lang
        raise ValueError("unknown language")

    return None
