import pytest

from app.commands import parse_group_command, parse_private_command, validate_args
from app.models import Cmd

BOT = "upb_bot"


def ent(text: str) -> tuple[tuple[str, int, int], ...]:
    """Build the bot_command entity for the leading /word token of text."""
    word = text.split()[0]
    return (("bot_command", 0, len(word)),)


# --- group parsing ---


@pytest.mark.parametrize(
    "text,expected_cmd,expected_args",
    [
        ("/upb chat register", Cmd.CHAT_REGISTER, ()),
        ("/upb chat register extra args", Cmd.CHAT_REGISTER, ("extra", "args")),
        ("/upb chat unregister", Cmd.CHAT_UNREGISTER, ()),
        ("/upb notify on", Cmd.NOTIFY_ON, ()),
        ("/upb notify off", Cmd.NOTIFY_OFF, ()),
        ("/upb notify all", Cmd.PING, ()),
        ("/upb all", Cmd.PING, ()),
        ("/upb list", Cmd.LIST, ()),
        ("/upb help", Cmd.HELP, ()),
        ("/upb usage", Cmd.HELP, ()),
    ],
)
def test_group_command_addressed_forms(text, expected_cmd, expected_args):
    parsed = parse_group_command(text, ent(text), bot_username=BOT)
    assert parsed is not None
    assert parsed.cmd is expected_cmd
    assert parsed.args == expected_args
    assert parsed.raw == text


def test_group_command_addressed_to_us_explicitly():
    text = f"/upb@{BOT} notify on"
    parsed = parse_group_command(text, ent(text), bot_username=BOT)
    assert parsed is not None
    assert parsed.cmd is Cmd.NOTIFY_ON


def test_group_command_addressed_to_different_bot_returns_none():
    text = "/upb@other_bot notify on"
    assert parse_group_command(text, ent(text), bot_username=BOT) is None


def test_group_command_case_insensitive_bot_suffix():
    text = f"/upb@{BOT.upper()} list"
    assert parse_group_command(text, ent(text), bot_username=BOT).cmd is Cmd.LIST


def test_bare_upb_with_no_subcommand_is_none():
    text = "/upb"
    assert parse_group_command(text, ent(text), bot_username=BOT) is None


def test_upb_with_unknown_subcommand_is_none():
    text = "/upb frobnicate"
    assert parse_group_command(text, ent(text), bot_username=BOT) is None


def test_text_starting_with_slash_but_no_entity_is_none():
    text = "/upb notify on"
    assert parse_group_command(text, (), bot_username=BOT) is None


def test_plain_text_is_none():
    assert parse_group_command("just talking", (), bot_username=BOT) is None
    assert parse_group_command("", (), bot_username=BOT) is None
    assert parse_group_command(None, (), bot_username=BOT) is None


def test_entity_not_at_offset_zero_is_ignored():
    text = "hey /upb notify on"
    entities = (("bot_command", 4, 4),)
    assert parse_group_command(text, entities, bot_username=BOT) is None


def test_other_command_name_in_group_is_none():
    text = "/help"
    assert parse_group_command(text, ent(text), bot_username=BOT) is None


# --- private parsing ---


@pytest.mark.parametrize(
    "text,expected_cmd",
    [
        ("/help", Cmd.P_HELP),
        ("/usage", Cmd.P_HELP),
        ("/start", Cmd.P_HELP),
        ("/admin create 123", Cmd.ADMIN_CREATE),
        ("/admin remove 123", Cmd.ADMIN_REMOVE),
        ("/admin list", Cmd.ADMIN_LIST),
        ("/chat list", Cmd.CHAT_LIST),
        ("/chat remove -100123", Cmd.CHAT_REMOVE),
    ],
)
def test_private_command_forms(text, expected_cmd):
    parsed = parse_private_command(text, ent(text), bot_username=BOT)
    assert parsed is not None
    assert parsed.cmd is expected_cmd


def test_private_admin_create_args():
    text = "/admin create 123"
    parsed = parse_private_command(text, ent(text), bot_username=BOT)
    assert parsed.args == ("123",)


def test_private_command_at_bot_suffix_accepted():
    text = f"/help@{BOT}"
    parsed = parse_private_command(text, ent(text), bot_username=BOT)
    assert parsed is not None and parsed.cmd is Cmd.P_HELP


def test_private_command_addressed_to_other_bot_is_none():
    text = "/help@other_bot"
    assert parse_private_command(text, ent(text), bot_username=BOT) is None


def test_private_admin_with_no_subcommand_is_none():
    text = "/admin"
    assert parse_private_command(text, ent(text), bot_username=BOT) is None


def test_private_admin_unknown_subcommand_is_none():
    text = "/admin frobnicate"
    assert parse_private_command(text, ent(text), bot_username=BOT) is None


def test_private_chat_with_no_subcommand_is_none():
    text = "/chat"
    assert parse_private_command(text, ent(text), bot_username=BOT) is None


def test_private_unknown_command_is_none():
    text = "/whoami"
    assert parse_private_command(text, ent(text), bot_username=BOT) is None


def test_private_plain_text_is_none():
    assert parse_private_command("hello there", (), bot_username=BOT) is None


# --- validate_args ---


@pytest.mark.parametrize("cmd", [Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE])
def test_validate_args_admin_positive_int_ok(cmd):
    assert validate_args(cmd, ("42",)) == 42


@pytest.mark.parametrize("cmd", [Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE])
def test_validate_args_admin_rejects_non_positive(cmd):
    with pytest.raises(ValueError):
        validate_args(cmd, ("0",))
    with pytest.raises(ValueError):
        validate_args(cmd, ("-5",))


@pytest.mark.parametrize("cmd", [Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE])
def test_validate_args_admin_rejects_bad_arity(cmd):
    with pytest.raises(ValueError):
        validate_args(cmd, ())
    with pytest.raises(ValueError):
        validate_args(cmd, ("1", "2"))


@pytest.mark.parametrize("cmd", [Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE])
def test_validate_args_admin_rejects_unparsable(cmd):
    with pytest.raises(ValueError):
        validate_args(cmd, ("abc",))


def test_validate_args_chat_remove_allows_negative():
    assert validate_args(Cmd.CHAT_REMOVE, ("-100987654321",)) == -100987654321


def test_validate_args_chat_remove_rejects_bad_arity_or_syntax():
    with pytest.raises(ValueError):
        validate_args(Cmd.CHAT_REMOVE, ())
    with pytest.raises(ValueError):
        validate_args(Cmd.CHAT_REMOVE, ("not_a_number",))


@pytest.mark.parametrize(
    "cmd",
    [
        Cmd.CHAT_REGISTER,
        Cmd.CHAT_UNREGISTER,
        Cmd.NOTIFY_ON,
        Cmd.NOTIFY_OFF,
        Cmd.PING,
        Cmd.LIST,
        Cmd.HELP,
        Cmd.P_HELP,
        Cmd.ADMIN_LIST,
        Cmd.CHAT_LIST,
    ],
)
def test_validate_args_other_commands_return_none(cmd):
    assert validate_args(cmd, ()) is None
    assert validate_args(cmd, ("whatever",)) is None


