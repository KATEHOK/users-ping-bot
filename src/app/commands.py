"""Pure command parsing: no I/O. Recognition only from a bot_command entity at offset 0."""

from collections.abc import Sequence

from .models import Cmd, ParsedCommand


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

    words = rest.split()
    if not words:
        return None
    w = [x.lower() for x in words]

    if w[0] == "chat" and len(w) >= 2 and w[1] == "register":
        return ParsedCommand(Cmd.CHAT_REGISTER, tuple(words[2:]), text or "")
    if w[0] == "chat" and len(w) >= 2 and w[1] == "unregister":
        return ParsedCommand(Cmd.CHAT_UNREGISTER, tuple(words[2:]), text or "")
    if w[0] == "notify" and len(w) >= 2 and w[1] == "on":
        return ParsedCommand(Cmd.NOTIFY_ON, tuple(words[2:]), text or "")
    if w[0] == "notify" and len(w) >= 2 and w[1] == "off":
        return ParsedCommand(Cmd.NOTIFY_OFF, tuple(words[2:]), text or "")
    if w[0] == "notify" and len(w) >= 2 and w[1] == "all":
        return ParsedCommand(Cmd.PING, tuple(words[2:]), text or "")
    if w[0] == "all":
        return ParsedCommand(Cmd.PING, tuple(words[1:]), text or "")
    if w[0] == "list":
        return ParsedCommand(Cmd.LIST, tuple(words[1:]), text or "")
    if w[0] in ("help", "usage"):
        return ParsedCommand(Cmd.HELP, tuple(words[1:]), text or "")
    return None


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
    words = rest.split()

    if name in ("help", "usage", "start"):
        return ParsedCommand(Cmd.P_HELP, tuple(words), text or "")

    if name == "admin":
        if not words:
            return None
        sub = words[0].lower()
        if sub == "create":
            return ParsedCommand(Cmd.ADMIN_CREATE, tuple(words[1:]), text or "")
        if sub == "remove":
            return ParsedCommand(Cmd.ADMIN_REMOVE, tuple(words[1:]), text or "")
        if sub == "list":
            return ParsedCommand(Cmd.ADMIN_LIST, tuple(words[1:]), text or "")
        return None

    if name == "chat":
        if not words:
            return None
        sub = words[0].lower()
        if sub == "list":
            return ParsedCommand(Cmd.CHAT_LIST, tuple(words[1:]), text or "")
        if sub == "remove":
            return ParsedCommand(Cmd.CHAT_REMOVE, tuple(words[1:]), text or "")
        return None

    return None


def validate_args(cmd: Cmd, args: Sequence[str]) -> int | None:
    """Parses positive ids for ADMIN_*, any int for CHAT_REMOVE. Raises ValueError on bad args."""
    if cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE):
        if len(args) != 1:
            raise ValueError("expected exactly one user_id argument")
        try:
            value = int(args[0])
        except ValueError:
            raise ValueError("user_id must be an integer") from None
        if value <= 0:
            raise ValueError("user_id must be positive")
        return value

    if cmd is Cmd.CHAT_REMOVE:
        if len(args) != 1:
            raise ValueError("expected exactly one chat_id argument")
        try:
            return int(args[0])
        except ValueError:
            raise ValueError("chat_id must be an integer") from None

    return None
