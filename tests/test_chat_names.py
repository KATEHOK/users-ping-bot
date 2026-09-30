"""Per-chat display names: /rename, migration 003, pings, lists, cascades."""

import pytest

import app.__main__ as entry
from app import commands, rendering
from app.delivery import PermanentSend
from app.db import MIGRATIONS_DIR, Database, _split_statements, apply_migrations
from app.handlers import handle_event
from app.models import Cmd, ParsedCommand
from app.rendering import t

from conftest import BOT_ID, BOT_USERNAME, find_html_errors, group_event, make_admin, make_event, make_root, mk_ctx, private_event, register_chat, subscribe

CHAT = 500
REGISTRAR = 1
ROOT = 10
SUB = 20
NOBODY = 40
EVIL = '<b>&"x"</b>'


async def _world(db, services):
    await make_root(db, services, ROOT)
    await make_admin(db, services, REGISTRAR)
    await register_chat(db, services, CHAT, REGISTRAR)
    await subscribe(db, services, CHAT, SUB)


async def _names(db):
    async with db.reader() as c:
        cursor = await c.execute("SELECT chat_id, user_id, name FROM chat_names ORDER BY 1, 2")
        return await cursor.fetchall()


async def _say(ctx, text, user=SUB, uid=[0], chat_id=CHAT, **kw):
    uid[0] += 1
    await handle_event(ctx, group_event(text, update_id=1000 + uid[0], user_id=user, chat_id=chat_id, **kw))


def _texts(transport):
    return [c["text"] for c in transport.calls]


# --- migration ---


async def test_migration_003_applies_on_a_db_at_002_and_is_idempotent(tmp_path):
    database = Database(str(tmp_path / "v11.sqlite3"))
    await database.connect()
    async with database.transaction() as c:
        for name in ("001_initial", "002_member_menus"):
            for stmt in _split_statements((MIGRATIONS_DIR / f"{name}.sql").read_text()):
                await c.execute(stmt)
            await c.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (name,))
        await c.execute("INSERT INTO users(user_id, updated_at) VALUES (5, 'x')")
    assert await apply_migrations(database) == ["003_chat_names"]
    assert await apply_migrations(database) == []
    async with database.reader() as c:
        cursor = await c.execute("SELECT COUNT(*) FROM users")
        assert (await cursor.fetchone())[0] == 1
        cursor = await c.execute("SELECT COUNT(*) FROM chat_names")
        assert (await cursor.fetchone())[0] == 0
    await database.close()


async def test_table_rejects_bad_length_and_unknown_chat_or_user(db):
    async def put(chat_id, user_id, name):
        async with db.transaction() as c:
            await c.execute(
                "INSERT INTO chat_names(chat_id, user_id, name, updated_at) VALUES (?, ?, ?, 't')",
                (chat_id, user_id, name),
            )

    ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await put(CHAT, SUB, "ok")
    for args in ((CHAT, REGISTRAR, ""), (CHAT, REGISTRAR, "x" * 65), (999, SUB, "x"), (CHAT, 777, "x")):
        with pytest.raises(Exception):
            await put(*args)


# --- parsing ---


@pytest.mark.parametrize(
    "text,args",
    [
        ("/rename Ann", (" Ann",)),
        ("/rename   Ann   Lee  ", ("   Ann   Lee  ",)),
        ("/rename@upb_bot Ann", (" Ann",)),
        ("/RENAME Ann", (" Ann",)),
        ("/upb rename Ann Lee", (" Ann Lee",)),
        ("/upb notify rename  Ann  Lee", ("  Ann  Lee",)),
        ("/UPB Notify Rename Ann", (" Ann",)),
        ("/rename", ()),
        ("/rename   ", ()),
        ("/upb rename", ()),
        ("/upb notify rename   ", ()),
    ],
)
def test_parse_rename_forms(text, args):
    ents = (("bot_command", 0, len(text.split(" ", 1)[0])),)
    parsed = commands.parse_group_command(text, ents, bot_username=BOT_USERNAME)
    assert parsed == ParsedCommand(Cmd.RENAME, args)
    assert parsed.via_alias is text.lower().startswith("/rename")


def test_parse_rename_for_another_bot_is_ignored():
    text = "/rename@other_bot Ann"
    ents = (("bot_command", 0, len("/rename@other_bot")),)
    assert commands.parse_group_command(text, ents, bot_username=BOT_USERNAME) is None


def test_upb_notify_without_a_known_word_is_still_usage():
    text = "/upb notify"
    parsed = commands.parse_group_command(text, (("bot_command", 0, 4),), bot_username=BOT_USERNAME)
    assert parsed.cmd is Cmd.USAGE


@pytest.mark.parametrize(
    "raw,name",
    [
        ("Ann", "Ann"),
        ("  Ann  ", "Ann"),
        ("Ann    Lee x", "Ann Lee x"),
        ("a" * 64, "a" * 64),
        ("\U0001f600" * 64, "\U0001f600" * 64),
        ("-", "-"),
        ("- x", "- x"),
        ("<b>&</b>", "<b>&</b>"),
    ],
)
def test_validate_name_accepts(raw, name):
    assert commands.validate_name(raw) == name


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "a" * 65,
        "\U0001f600" * 65,
        "a\nb",
        "a\tb",
        "a\x00b",
        "a\x1fb",
        "a\x7fb",
        "a\x85b",
        "a b",
        "a b",
        "a‪b",
        "a‮b",
        "a⁦b",
        "a⁩b",
        "‮",
        # nothing visible: format, bare marks, spaces, blank fillers, bidi marks
        "\u200b", "\u2060", "\ufeff", "\u00ad", "\u200e", "\u200f", "\u061c",
        "\u0301", "\u034f", "\u20dd", "\u115f", "\u1160", "\u3164", "\uffa0", "\u2800", "\u180e",
        "\u200b\u2060\u3164\u0301\u2800", "\u00a0\u200b \u3000",
        # bidi marks are rejected anywhere
        "a\u200eb", "a\u200fb", "a\u061cb",
    ],
)
def test_validate_name_rejects(raw):
    with pytest.raises(ValueError):
        commands.validate_name(raw)


@pytest.mark.parametrize(
    "raw",
    ["a\u200b", "\u200bx", "e\u0301", "\u0301e", "a\u3164", "\u2800b", "\U0001f468\u200d\U0001f469\u200d\U0001f467"],
)
def test_validate_name_accepts_a_visible_character_among_invisible_ones(raw):
    assert commands.validate_name(raw) == raw


def test_length_counts_code_points_after_normalisation():
    assert commands.validate_name("a " + " " * 10 + "b" * 62) == "a " + "b" * 62  # 64
    with pytest.raises(ValueError):
        commands.validate_name("a " + "b" * 63)  # 65


# --- the command ---


@pytest.mark.parametrize("form", ["/rename Ann", "/rename@upb_bot Ann", "/upb rename Ann", "/upb notify rename Ann"])
async def test_every_form_sets_the_name(db, form):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, form)
    assert await _names(db) == [(CHAT, SUB, "Ann")]
    assert _texts(transport) == [t("rename_set", "en", name="Ann")]


async def test_other_bot_is_ignored(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename@other_bot Ann")
    assert await _names(db) == [] and transport.calls == []


async def test_inner_text_is_normalised_not_split(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename   Ann    de   Lee  ")
    assert await _names(db) == [(CHAT, SUB, "Ann de Lee")]


async def test_reply_is_short_and_escaped(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, f"/rename {EVIL}")
    assert _texts(transport) == ["Name in this chat: &lt;b&gt;&amp;\"x\"&lt;/\u2060b&gt;."]


async def test_reply_language_follows_the_chat(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.set_chat_lang(c, CHAT, "ru")
    await _say(ctx, "/rename Ann")
    assert _texts(transport) == ["Имя в этом чате: Ann."]


async def test_second_rename_replaces_the_first(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    await _say(ctx, "/rename Bob")
    assert await _names(db) == [(CHAT, SUB, "Bob")]


@pytest.mark.parametrize("who", [SUB, ROOT, REGISTRAR])
async def test_subscriber_and_owners_may_rename(db, who):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)  # ROOT and REGISTRAR are not subscribed
    await _say(ctx, "/rename Ann", user=who)
    assert await _names(db) == [(CHAT, who, "Ann")]
    assert len(transport.calls) == 1


@pytest.mark.parametrize("who", [NOBODY, 30])
async def test_others_are_denied_silently(db, who):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await make_admin(db, services, 30)  # a foreign admin owns nothing here
    for form in ("/rename Ann", "/rename", "/upb rename Ann", "/upb notify rename Ann", "/rename -"):
        await _say(ctx, form, user=who)
    assert await _names(db) == [] and transport.calls == [] and transport.menu_calls == []


async def test_unregistered_group_is_silent_even_for_staff(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann", user=ROOT, chat_id=-900)
    assert await _names(db) == [] and transport.calls == [] and transport.menu_calls == []


async def test_private_chat_makes_no_reaction(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, private_event("/rename Ann", update_id=5, user_id=ROOT))
    await handle_event(ctx, private_event("/upb rename Ann", update_id=6, user_id=ROOT))
    assert await _names(db) == [] and transport.calls == []


async def test_bare_rename_shows_usage_and_the_telegram_name_and_changes_nothing(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename", display_name="Tg Name")
    [text] = _texts(transport)
    assert text == t("rename_usage", "en", syntax="/rename <name>", name="Tg Name")
    assert "<code>/rename &lt;name&gt;</code>" in text and "Tg Name" in text
    assert await _names(db) == []


async def test_bare_rename_shows_the_chat_name_and_the_syntax_form(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    transport.calls.clear()
    await _say(ctx, "/upb notify rename")
    [text] = _texts(transport)
    assert "<code>/upb notify rename &lt;name&gt;</code>" in text and "Current name: Ann" in text
    await _say(ctx, "/upb rename   ")
    assert "Current name: Ann" in _texts(transport)[1]
    assert await _names(db) == [(CHAT, SUB, "Ann")]


async def test_bare_rename_without_any_name_shows_the_id(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename", user=ROOT, display_name=None)
    assert f"id{ROOT}" in _texts(transport)[0]


async def test_dash_resets_to_the_telegram_name(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    transport.calls.clear()
    await _say(ctx, "/rename -", display_name="Tg<1>")
    assert await _names(db) == []
    assert _texts(transport) == ["Name reset: Tg&lt;1&gt;."]
    await _say(ctx, "/rename -")  # nothing to reset: still fine
    assert len(transport.calls) == 2


@pytest.mark.parametrize("form", ["/rename -", "/upb rename  -  ", "/upb notify rename -"])
async def test_reset_forms(db, form):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    await _say(ctx, form)
    assert await _names(db) == []


@pytest.mark.parametrize(
    "arg",
    ["a" * 65, "\U0001f600" * 65, "a\tb", "a‮b", "a b", "a⁧b", "a\x01b"],
)
async def test_rejected_names_reply_with_limits_and_change_nothing(db, arg):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    transport.calls.clear()
    await _say(ctx, f"/rename {arg}")
    assert _texts(transport) == [t("rename_bad", "en", syntax="/rename <name>")]
    assert "1-64" in _texts(transport)[0] and "<code>/rename &lt;name&gt;</code>" in _texts(transport)[0]
    await _say(ctx, f"/upb rename {arg}")
    assert "<code>/upb notify rename &lt;name&gt;</code>" in _texts(transport)[1]
    assert await _names(db) == [(CHAT, SUB, "Ann")]


async def test_inner_newline_is_rejected_but_trailing_one_is_not(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann\nLee")
    assert await _names(db) == []
    await _say(ctx, "/rename Ann\n")
    assert await _names(db) == [(CHAT, SUB, "Ann")]


async def test_boundary_64_accepted_65_rejected(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename " + "\U0001f600" * 64)
    assert (await _names(db))[0][2] == "\U0001f600" * 64
    await _say(ctx, "/rename " + "b" * 65)
    assert (await _names(db))[0][2] == "\U0001f600" * 64


async def test_an_ignored_update_is_not_recorded_as_error(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename " + "a" * 65)
    async with db.reader() as c:
        cursor = await c.execute("SELECT outcome FROM processed_updates")
        assert [r[0] for r in await cursor.fetchall()] == ["ok"]


# --- where the name shows ---


async def test_ping_mention_uses_the_chat_name_escaped(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await subscribe(db, services, CHAT, 21)
    await _say(ctx, f"/rename {EVIL}")
    transport.calls.clear()
    await _say(ctx, "/all", user=21)
    text = _texts(transport)[0]
    assert f'<a href="tg://user?id={SUB}">&lt;b&gt;&amp;"x"&lt;/b&gt;</a>' in text
    assert "User20" not in text


async def test_ping_uses_the_telegram_name_without_a_chat_name(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await subscribe(db, services, CHAT, 21)
    await _say(ctx, "/all", user=21)
    assert f'<a href="tg://user?id={SUB}">U{SUB}</a>' in _texts(transport)[0]


async def test_ping_snapshot_takes_the_name_in_the_same_transaction(db):
    _ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.set_chat_name(c, CHAT, SUB, "Ann")
        snap = await services.list_subscribers(c, CHAT)
    assert [(s.user_id, s.display_name) for s in snap] == [(SUB, "Ann")]


async def test_names_are_per_chat(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, 600, REGISTRAR)
    await subscribe(db, services, 600, SUB)
    await _say(ctx, "/rename Ann")
    async with db.reader() as c:
        assert [s.display_name for s in await services.list_subscribers(c, 600)] == [f"User{SUB}"]


async def test_list_shows_only_the_chat_name_escaped(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, f"/rename {EVIL}@x", username="u_name")
    transport.calls.clear()
    await _say(ctx, "/list", username="u_name")
    assert _texts(transport) == [f"&lt;b&gt;&amp;\"x\"&lt;/\u2060b&gt;＠x - {SUB}"]


async def test_off_and_on_keep_the_name(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    await _say(ctx, "/off")
    assert await _names(db) == [(CHAT, SUB, "Ann")]
    await _say(ctx, "/on")
    assert await _names(db) == [(CHAT, SUB, "Ann")]
    async with db.reader() as c:
        assert [s.display_name for s in await services.list_subscribers(c, CHAT)] == ["Ann"]


async def test_a_name_set_by_an_unsubscribed_owner_applies_after_subscribing(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Boss", user=ROOT)
    await _say(ctx, "/on", user=ROOT)
    async with db.reader() as c:
        assert "Boss" in [s.display_name for s in await services.list_subscribers(c, CHAT)]


# --- cascades ---


async def _named(db, services, chat_id=CHAT):
    async with db.transaction() as c:
        await services.set_chat_name(c, chat_id, SUB, "Ann")


async def test_unregister_chat_drops_names(db):
    _ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await _named(db, services)
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT)
    assert await _names(db) == []


async def test_remove_chat_drops_names(db):
    _ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await _named(db, services)
    async with db.transaction() as c:
        await services.remove_chat(c, CHAT)
    assert await _names(db) == []


async def test_revoke_admin_drops_names_of_his_chats(db):
    _ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await _named(db, services)
    async with db.transaction() as c:
        await services.revoke_admin(c, REGISTRAR)
    assert await _names(db) == []


async def test_set_root_drops_names_of_the_previous_root_chats(db):
    _ctx, services, _t, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    await register_chat(db, services, CHAT, ROOT)
    await subscribe(db, services, CHAT, SUB)
    await _named(db, services)
    await make_admin(db, services, 11)
    async with db.transaction() as c:
        await services.set_root(c, 11)
    assert await _names(db) == []


async def test_unregister_command_drops_names(db):
    ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    await _say(ctx, "/unregister", user=REGISTRAR)
    assert await _names(db) == []


async def test_reregistering_starts_without_names(db):
    ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await _named(db, services)
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT)
    await register_chat(db, services, CHAT, REGISTRAR)
    await subscribe(db, services, CHAT, SUB)
    assert await _names(db) == []


# --- chat migration ---


async def test_migration_moves_names_to_the_new_id(db):
    ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await subscribe(db, services, CHAT, 21)
    await _named(db, services)
    async with db.transaction() as c:
        await services.set_chat_name(c, CHAT, 21, "Bob")
        result = await services.migrate_chat(c, CHAT, -1000)
    assert result.action == "moved"
    assert await _names(db) == [(-1000, SUB, "Ann"), (-1000, 21, "Bob")]


async def test_migration_keeps_the_destination_names_when_it_is_registered(db):
    _ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, -1000, REGISTRAR)
    await subscribe(db, services, -1000, SUB)
    async with db.transaction() as c:
        await services.set_chat_name(c, CHAT, SUB, "Old")
        await services.set_chat_name(c, -1000, SUB, "New")
        result = await services.migrate_chat(c, CHAT, -1000)
    assert result.action == "kept_destination"
    assert await _names(db) == [(-1000, SUB, "New")]


async def test_migration_of_an_unregistered_chat_leaves_no_names(db):
    _ctx, services, _t, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        result = await services.migrate_chat(c, -900, -901)
    assert result.action == "alias_only"
    assert await _names(db) == []


async def test_rename_after_migration_through_the_old_id_is_ignored_and_new_id_works(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.migrate_chat(c, CHAT, -1000)
    await _say(ctx, "/rename Ann")  # the old id: ignored
    assert await _names(db) == [] and transport.calls == []
    await _say(ctx, "/rename Ann", chat_id=-1000)
    assert await _names(db) == [(-1000, SUB, "Ann")]


# --- menu, help ---


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_menu_has_rename_in_chat_and_owner_scopes(lang):
    for menu in (rendering.menu_commands(lang), rendering.owner_menu_commands(lang)):
        entries = dict(menu)
        assert "rename" in entries and 3 <= len(entries["rename"]) <= 32
    assert "rename" not in dict(rendering.register_menu_commands(lang))


async def test_registering_sets_a_chat_menu_with_rename(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    await _say(ctx, "/register", user=ROOT)
    chat_calls = [c for c in transport.menu_calls if c["user_id"] is None and c["op"] == "set"]
    assert chat_calls and "rename" in [n for n, _d in chat_calls[0]["commands"]]


@pytest.mark.parametrize("lang,desc", [("en", "Set name"), ("ru", "Задать имя")])
async def test_help_and_usage_list_the_alias_as_plain_text(db, lang, desc):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.set_chat_lang(c, CHAT, lang)
    await _say(ctx, "/help")
    await _say(ctx, "/upb notify")
    for text in _texts(transport):
        assert f"/rename &lt;{'имя' if lang == 'ru' else 'name'}&gt; — {desc}" in text
        assert "<pre>" not in text
    transport.calls.clear()
    await _say(ctx, "/help", user=NOBODY)
    assert transport.calls == []


# --- inert rendering (F3) ---


@pytest.mark.parametrize("lang", ["en", "ru"])
async def test_replies_render_the_name_inert(db, lang):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.set_chat_lang(c, CHAT, lang)
    await _say(ctx, "/rename @boss /unregister")
    await _say(ctx, "/rename")
    set_text, usage_text = _texts(transport)
    for text in (set_text, usage_text):
        assert "\uff20boss" in text and "@boss" not in text
        assert "/\u2060unregister" in text and "/unregister" not in text
    await _say(ctx, "/rename -", display_name="/x @y")
    reset_text = _texts(transport)[2]
    assert "/\u2060x \uff20y" in reset_text and "@y" not in reset_text and "/x" not in reset_text
    assert all(find_html_errors(x) == [] for x in _texts(transport))


async def test_list_renders_a_leading_command_inert(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename /unregister")
    transport.calls.clear()
    await _say(ctx, "/list")
    assert _texts(transport) == [f"/\u2060unregister - {SUB}"]


async def test_ping_mention_text_is_not_made_inert(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await subscribe(db, services, CHAT, 21)
    await _say(ctx, "/rename /x@y")
    transport.calls.clear()
    await _say(ctx, "/all", user=21)
    assert f'<a href="tg://user?id={SUB}">/x@y</a>' in _texts(transport)[0]


async def test_rename_with_an_invisible_name_is_rejected_and_changes_nothing(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/rename Ann")
    await _say(ctx, "/rename \u200b\u3164")
    assert await _names(db) == [(CHAT, SUB, "Ann")]
    assert _texts(transport)[-1] == t("rename_bad", "en", syntax="/rename <name>")


async def test_ru_syntax_uses_the_ru_placeholder(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.set_chat_lang(c, CHAT, "ru")
    await _say(ctx, "/rename")
    await _say(ctx, "/upb notify rename \u200b")
    await _say(ctx, "/upb rename")
    texts = "\n".join(_texts(transport))
    assert "<code>/rename &lt;\u0438\u043c\u044f&gt;</code>" in texts
    assert "<code>/upb notify rename &lt;\u0438\u043c\u044f&gt;</code>" in texts
    assert "&lt;name&gt;" not in texts


# --- names go with the chat (F8) ---


async def test_bot_removed_from_a_registered_chat_drops_its_names(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, 600, REGISTRAR)
    await subscribe(db, services, 600, SUB)
    await _say(ctx, "/rename Ann")
    await _say(ctx, "/rename Bob", chat_id=600)
    await handle_event(
        ctx,
        make_event(
            kind="my_chat_member", update_id=1, chat_id=CHAT, user_id=REGISTRAR,
            chat_type="group", bot_removed=True, left_user_id=BOT_ID, text=None,
        ),
    )
    assert await _names(db) == [(600, SUB, "Bob")]


async def test_startup_reconcile_of_a_gone_chat_drops_its_names(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, 600, REGISTRAR)
    await subscribe(db, services, 600, SUB)
    await _say(ctx, "/rename Ann")
    await _say(ctx, "/rename Bob", chat_id=600)
    transport.set_probe(CHAT, PermanentSend())
    assert await entry.reconcile_chats(ctx, transport) == [CHAT]
    assert await _names(db) == [(600, SUB, "Bob")]
