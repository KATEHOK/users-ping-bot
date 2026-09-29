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
        ("/upb notify list", Cmd.LIST, ()),
        ("/upb notify list x", Cmd.LIST, ("x",)),
        ("/upb register", Cmd.CHAT_REGISTER, ()),
        ("/upb register extra args", Cmd.CHAT_REGISTER, ("extra", "args")),
        ("/upb unregister", Cmd.CHAT_UNREGISTER, ()),
        ("/upb lang ru", Cmd.LANG, ("ru",)),
    ],
)
def test_group_command_addressed_forms(text, expected_cmd, expected_args):
    parsed = parse_group_command(text, ent(text), bot_username=BOT)
    assert parsed is not None
    assert parsed.cmd is expected_cmd
    assert parsed.args == expected_args


@pytest.mark.parametrize(
    "text,expected_cmd,expected_args",
    [
        ("/all", Cmd.PING, ()),
        ("/all foo Bar", Cmd.PING, ("foo", "Bar")),
        ("/on", Cmd.NOTIFY_ON, ()),
        ("/off", Cmd.NOTIFY_OFF, ()),
        ("/off extra", Cmd.NOTIFY_OFF, ("extra",)),
        ("/help", Cmd.HELP, ()),
        ("/usage", Cmd.HELP, ()),
        ("/list", Cmd.LIST, ()),
        ("/list x", Cmd.LIST, ("x",)),
        ("/register", Cmd.CHAT_REGISTER, ()),
        ("/register x", Cmd.CHAT_REGISTER, ("x",)),
        ("/unregister", Cmd.CHAT_UNREGISTER, ()),
        ("/lang ru", Cmd.LANG, ("ru",)),
        ("/lang ru extra", Cmd.LANG, ("ru", "extra")),
        ("/lang", Cmd.USAGE, ("lang",)),
        ("/ALL", Cmd.PING, ()),
        (f"/register@{BOT}", Cmd.CHAT_REGISTER, ()),
        (f"/all@{BOT}", Cmd.PING, ()),
        (f"/on@{BOT.upper()} x", Cmd.NOTIFY_ON, ("x",)),
    ],
)
def test_group_aliases(text, expected_cmd, expected_args):
    parsed = parse_group_command(text, ent(text), bot_username=BOT)
    assert parsed is not None
    assert (parsed.cmd, parsed.args) == (expected_cmd, expected_args)


@pytest.mark.parametrize("text", [
        "/all@other_bot",
        "/on@other_bot",
        "/help@other_bot x",
        "/list@other_bot",
        "/register@other_bot",
        "/unregister@other_bot",
        "/lang@other_bot ru",
    ],)
def test_group_alias_addressed_to_different_bot_is_none(text):
    assert parse_group_command(text, ent(text), bot_username=BOT) is None


def test_group_alias_needs_the_entity():
    assert parse_group_command("/all", (), bot_username=BOT) is None


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
    text = "/admin"
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




# --- prefix help ---


@pytest.mark.parametrize(
    "text,prefix",
    [
        ("/upb", ()),
        ("/upb qwe", ("qwe",)),
        ("/UPB Qwe extra", ("qwe",)),
        ("/upb notify", ("notify",)),
        ("/upb notify xyz", ("notify",)),
        ("/upb chat", ("chat",)),
        ("/upb chat xyz", ("chat",)),
        ("/upb lang", ("lang",)),
    ],
)
def test_group_prefix_help(text, prefix):
    parsed = parse_group_command(text, ent(text), bot_username=BOT)
    assert parsed is not None and parsed.cmd is Cmd.USAGE
    assert parsed.args == prefix


def test_group_prefix_help_respects_bot_addressing():
    text = f"/upb@{BOT}"
    assert parse_group_command(text, ent(text), bot_username=BOT).cmd is Cmd.USAGE
    other = "/upb@other_bot"
    assert parse_group_command(other, ent(other), bot_username=BOT) is None


def test_typo_in_command_name_is_not_ours():
    text = "/ubb notify on"
    assert parse_group_command(text, ent(text), bot_username=BOT) is None


@pytest.mark.parametrize(
    "text,prefix",
    [
        ("/admin", ("admin",)),
        ("/admin xyz", ("admin",)),
        ("/chat", ("chat",)),
        ("/chat xyz", ("chat",)),
        ("/lang", ("lang",)),
    ],
)
def test_private_prefix_help(text, prefix):
    parsed = parse_private_command(text, ent(text), bot_username=BOT)
    assert parsed is not None and parsed.cmd is Cmd.P_USAGE
    assert parsed.args == prefix


def test_private_prefix_help_wrong_bot_is_none():
    text = "/admin@other_bot"
    assert parse_private_command(text, ent(text), bot_username=BOT) is None


# --- lang ---


def test_group_lang_parses_with_code_argument():
    text = "/upb lang ru"
    parsed = parse_group_command(text, ent(text), bot_username=BOT)
    assert parsed.cmd is Cmd.LANG and parsed.args == ("ru",)


def test_private_lang_parses_with_code_argument():
    text = "/lang en"
    parsed = parse_private_command(text, ent(text), bot_username=BOT)
    assert parsed.cmd is Cmd.P_LANG and parsed.args == ("en",)


@pytest.mark.parametrize("cmd", [Cmd.LANG, Cmd.P_LANG])
def test_validate_lang(cmd):
    assert validate_args(cmd, ("en",)) == "en"
    assert validate_args(cmd, ("RU",)) == "ru"
    for bad in ((), ("de",), ("en", "ru"), ("",), ("e n",)):
        with pytest.raises(ValueError):
            validate_args(cmd, bad)


# --- strict ids ---

BAD_IDS = [
    "1_000",
    "+5",
    " 5",
    "5 ",
    "5\n",
    "1.0",
    "0x10",
    "\u0663\u0664",  # Arabic-Indic digits
    "\uff11\uff12",  # fullwidth digits
    "",
    "-",
    "9223372036854775808",
    "-9223372036854775809",
    "9" * 5000,
]


@pytest.mark.parametrize("bad", BAD_IDS)
@pytest.mark.parametrize("cmd", [Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.CHAT_REMOVE])
def test_validate_rejects_malformed_or_overflowing_ids(cmd, bad):
    with pytest.raises(ValueError):
        validate_args(cmd, (bad,))


def test_validate_int64_bounds_are_accepted():
    assert validate_args(Cmd.ADMIN_CREATE, ("9223372036854775807",)) == 2**63 - 1
    assert validate_args(Cmd.CHAT_REMOVE, ("-9223372036854775808",)) == -(2**63)


def test_admin_ids_must_be_strictly_positive_but_chat_ids_may_be_zero_or_negative():
    for cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE):
        for bad in ("0", "-1", "-0"):
            with pytest.raises(ValueError):
                validate_args(cmd, (bad,))
    assert validate_args(Cmd.CHAT_REMOVE, ("0",)) == 0
    assert validate_args(Cmd.CHAT_REMOVE, ("-100123",)) == -100123


def test_permission_is_separate_from_syntax():
    # parsing never validates arguments: a malformed id still parses as the command
    text = "/admin create 1_000"
    parsed = parse_private_command(text, ent(text), bot_username=BOT)
    assert parsed.cmd is Cmd.ADMIN_CREATE and parsed.args == ("1_000",)


@pytest.mark.parametrize("text", ["/list", "/register", "/unregister", "/all", "/on", "/off"])
def test_group_aliases_are_not_private_commands(text):
    assert parse_private_command(text, ent(text), bot_username=BOT) is None


def test_private_lang_is_unchanged_by_the_group_alias():
    text = "/lang ru"
    assert parse_private_command(text, ent(text), bot_username=BOT).cmd is Cmd.P_LANG
