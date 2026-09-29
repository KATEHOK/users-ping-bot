import re

import pytest

from app import rendering
from app.delivery import AmbiguousSend, PermanentSend, RateLimited
from app.handlers import handle_event
from app.models import Actor, Cmd, Role, Scope
from app.rendering import t

from conftest import (
    group_event,
    make_admin,
    make_root,
    mk_ctx,
    register_chat,
    subscribe,
)

CHAT = 500
REGISTRAR = 1  # admin who registered CHAT
ROOT = 10
SUB = 20  # subscriber of CHAT, no role
FOREIGN_ADMIN = 30  # admin, registered nothing here, not subscribed
NOBODY = 40

TEXTS = {
    Cmd.CHAT_REGISTER: "/upb chat register",
    Cmd.CHAT_UNREGISTER: "/upb chat unregister",
    Cmd.NOTIFY_ON: "/upb notify on",
    Cmd.NOTIFY_OFF: "/upb notify off",
    Cmd.PING: "/upb all",
    Cmd.LIST: "/upb list",
    Cmd.HELP: "/upb help",
    Cmd.LANG: "/upb lang ru",
}

# every spelling of each command: the rights and replies must not depend on it
FORMS = {
    Cmd.CHAT_REGISTER: ["/upb chat register", "/upb register", "/register"],
    Cmd.CHAT_UNREGISTER: ["/upb chat unregister", "/upb unregister", "/unregister"],
    Cmd.NOTIFY_ON: ["/upb notify on", "/on"],
    Cmd.NOTIFY_OFF: ["/upb notify off", "/off"],
    Cmd.PING: ["/upb all", "/upb notify all", "/all"],
    Cmd.LIST: ["/upb list", "/upb notify list", "/list"],
    Cmd.HELP: ["/upb help", "/upb usage", "/help", "/usage"],
    Cmd.LANG: ["/upb lang ru", "/lang ru"],
}

WHO = {"root": ROOT, "owner": REGISTRAR, "foreign_admin": FOREIGN_ADMIN, "subscriber": SUB, "nobody": NOBODY}
OWNERS = {"root", "owner"}
MEMBERS = OWNERS | {"subscriber"}

# active chat: who gets any reaction (reply or state change)
ACTIVE_ALLOWED = {
    Cmd.CHAT_REGISTER: OWNERS,
    Cmd.CHAT_UNREGISTER: OWNERS,
    Cmd.NOTIFY_ON: set(WHO),
    Cmd.NOTIFY_OFF: MEMBERS,
    Cmd.PING: MEMBERS,
    Cmd.LIST: MEMBERS,
    Cmd.HELP: MEMBERS,
    Cmd.LANG: OWNERS,
}
STAFF = {"root", "owner", "foreign_admin"}
INACTIVE_ALLOWED = {cmd: set() for cmd in TEXTS}
INACTIVE_ALLOWED[Cmd.CHAT_REGISTER] = STAFF
INACTIVE_ALLOWED[Cmd.HELP] = STAFF


async def _world(db, services, *, active: bool):
    await make_root(db, services, ROOT)
    await make_admin(db, services, REGISTRAR)
    await make_admin(db, services, FOREIGN_ADMIN)
    if active:
        await register_chat(db, services, CHAT, REGISTRAR)
        await subscribe(db, services, CHAT, SUB)
        await subscribe(db, services, CHAT, 21)


async def _send(ctx, text, user, uid, **kw):
    await handle_event(ctx, group_event(text, update_id=uid, user_id=user, **kw))


async def _texts(transport):
    return [c["text"] for c in transport.calls]


async def _count(db, sql, *params):
    async with db.reader() as c:
        cur = await c.execute(sql, params)
        return (await cur.fetchone())[0]


# --- rights matrix: a denied command makes zero transport calls ---


def _assert_matrix_reply(cmd: Cmd, who: str, active: bool, texts: list[str]) -> None:
    """The exact reply an allowed sender gets as the first command in the _world chat."""
    if not active and cmd is not Cmd.HELP:
        assert cmd is Cmd.CHAT_REGISTER
        assert texts == [rendering.welcome_text("en")]
        return
    assert len(texts) == 1, texts
    text = texts[0]
    is_sub = who == "subscriber"
    if cmd is Cmd.CHAT_REGISTER:
        assert text == t("already_registered", "en")
    elif cmd is Cmd.NOTIFY_ON:
        assert text == t("already_subscribed" if is_sub else "subscribed", "en")
    elif cmd is Cmd.NOTIFY_OFF:
        assert text == t("unsubscribed" if is_sub else "not_subscribed", "en")
    elif cmd is Cmd.PING:
        expected = {SUB, 21} - ({SUB} if is_sub else set())  # the sender is never pinged
        assert {int(i) for i in re.findall(r"tg://user\?id=(\d+)", text)} == expected
        assert text.count("<a ") == len(expected)
    elif cmd is Cmd.LIST:
        sub_line = f"User{SUB} (user{SUB}) - {SUB}" if is_sub else f"U{SUB} - {SUB}"  # sender is touched
        assert set(text.split("\n")) == {sub_line, "U21 - 21"}
    elif cmd is Cmd.HELP:
        assert text.startswith(t("help_title", "en") + "\n")
        if active:
            assert "/register" not in text  # active chat: not advertised
            assert ("/unregister" in text) == (who in OWNERS)
            assert ("/lang" in text) == (who in OWNERS)
        else:  # staff in a free chat: what they can run there
            assert "/register" in text and "/help" in text
            assert "/unregister" not in text and "/all" not in text
    elif cmd is Cmd.LANG:
        assert text == t("lang_set", "ru")
    else:
        raise AssertionError(cmd)


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("who", list(WHO))
@pytest.mark.parametrize(
    "cmd,text", [(cmd, text) for cmd, texts in FORMS.items() for text in texts]
)
async def test_rights_matrix(db, cmd, text, who, active):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=active)
    table = ACTIVE_ALLOWED if active else INACTIVE_ALLOWED

    await _send(ctx, text, WHO[who], 1)

    if who in table[cmd]:
        # an unregister answers through the outbox, everything else with a reply
        if cmd is Cmd.CHAT_UNREGISTER:
            assert transport.calls == []
            assert await _count(db, "SELECT COUNT(*) FROM outbox") == 1
        else:
            _assert_matrix_reply(cmd, who, active, await _texts(transport))
    else:
        assert transport.calls == []
        assert transport.menu_calls == []  # a denied command touches nothing at Telegram
        assert await _count(db, "SELECT COUNT(*) FROM outbox") == 0
        # still committed: the claim and the contact record
        assert await _count(db, "SELECT outcome FROM processed_updates WHERE update_id = 1") == "ignored"
        assert await _count(db, "SELECT COUNT(*) FROM users WHERE user_id = ?", WHO[who]) == 1
        if active:
            async with db.reader() as c:
                assert await services.get_chat(c, CHAT) is not None
                assert await services.is_subscribed(c, CHAT, SUB)
                assert (await services.get_chat(c, CHAT)).lang == "en"


async def test_denied_new_user_is_still_recorded(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb all", 777, 1)
    assert transport.calls == []
    assert await _count(db, "SELECT COUNT(*) FROM users WHERE user_id = 777") == 1


# --- register / unregister ---


async def test_register_replies_welcome_and_owner_gets_already_registered(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, REGISTRAR)
    await _send(ctx, "/upb chat register", REGISTRAR, 1)
    await _send(ctx, "/upb chat register", REGISTRAR, 2)
    assert await _texts(transport) == [
        "Chat registered. Subscribe: /on. Ping: /all. Help: /help.",
        "Chat is already registered.",
    ]
    async with db.reader() as c:
        assert (await services.get_chat(c, CHAT)).registered_by == REGISTRAR


async def test_root_reregistration_does_not_change_the_registrar(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb chat register", ROOT, 1)
    assert await _texts(transport) == ["Chat is already registered."]
    async with db.reader() as c:
        assert (await services.get_chat(c, CHAT)).registered_by == REGISTRAR


async def test_unregister_drops_subscriptions_and_queues_farewell_in_chat_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb lang ru", REGISTRAR, 1)
    transport.calls.clear()
    await _send(ctx, "/upb chat unregister", REGISTRAR, 2)
    assert transport.calls == []
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert not await services.is_subscribed(c, CHAT, SUB)
        ev = (await services.due_events(c, "9999"))[0]
    assert ev.payload == {"lang": "ru", "owners": [ROOT, REGISTRAR]}
    await ctx.delivery.run_outbox_once()
    assert await _texts(transport) == ["\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0447\u0430\u0442\u0430 \u0441\u043d\u044f\u0442\u0430. \u0414\u043e \u0432\u0441\u0442\u0440\u0435\u0447\u0438!"]
    # the admin keeps the role
    async with db.reader() as c:
        assert (await services.get_role(c, REGISTRAR)) is not None


async def test_reregistration_after_unregister_resets_language_to_en(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb lang ru", REGISTRAR, 1)
    await _send(ctx, "/upb chat unregister", REGISTRAR, 2)
    await _send(ctx, "/upb chat register", REGISTRAR, 3)
    assert (await _texts(transport))[-1].startswith("Chat registered.")


# --- notify ---


async def test_notify_on_twice_and_off_variants(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb notify on", NOBODY, 1)
    await _send(ctx, "/upb notify on", NOBODY, 2)
    await _send(ctx, "/upb notify off", NOBODY, 3)
    await _send(ctx, "/upb notify off", NOBODY, 4)  # no longer a subscriber: silence
    await _send(ctx, "/upb notify off", REGISTRAR, 5)  # owner without subscription
    assert await _texts(transport) == [
        "Subscribed.",
        "Already subscribed.",
        "Unsubscribed.",
        "Not subscribed.",
    ]


async def test_foreign_admin_subscribes_and_then_has_subscriber_rights(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb list", FOREIGN_ADMIN, 1)
    assert transport.calls == []
    await _send(ctx, "/upb notify on", FOREIGN_ADMIN, 2)
    await _send(ctx, "/upb list", FOREIGN_ADMIN, 3)
    assert len(transport.calls) == 2
    await _send(ctx, "/upb chat unregister", FOREIGN_ADMIN, 4)
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is not None  # not an owner


# --- ping ---


def _ids(text):
    return [int(m) for m in re.findall(r'tg://user\?id=(\d+)"', text)]


async def test_ping_excludes_the_initiator_even_when_subscribed(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb all", SUB, 1, message_id=77, thread_id=9)
    assert [_ids(t) for t in await _texts(transport)] == [[21]]
    assert transport.calls[0]["reply_to_message_id"] == 77
    assert transport.calls[0]["thread_id"] == 9


async def test_all_alias_pings_like_the_full_form(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb all", REGISTRAR, 1)
    await _send(ctx, "/all", ROOT, 2)
    first, second = await _texts(transport)
    assert _ids(first) == _ids(second) == [SUB, 21]


async def test_all_alias_from_a_non_subscriber_is_silent(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/all", NOBODY, 1)
    assert transport.calls == []


async def test_short_on_off_help_aliases(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/on", NOBODY, 1)
    await _send(ctx, "/help", NOBODY, 2)
    await _send(ctx, "/off", NOBODY, 3)
    texts = await _texts(transport)
    assert texts[0] == t("subscribed", "en") and texts[2] == t("unsubscribed", "en")
    assert texts[1].startswith(t("help_title", "en") + "\n")


async def test_ping_of_only_the_initiator_answers_pong(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, REGISTRAR)
    await register_chat(db, services, CHAT, REGISTRAR)
    await subscribe(db, services, CHAT, SUB)
    await _send(ctx, "/upb notify all", SUB, 1)
    assert await _texts(transport) == ["pong"]


async def test_owner_pings_all_subscribers_and_empty_chat_gets_pong(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb all", REGISTRAR, 1)
    assert _ids(transport.calls[0]["text"]) == [SUB, 21]
    async with db.transaction() as c:
        await services.unsubscribe(c, CHAT, SUB)
        await services.unsubscribe(c, CHAT, 21)
    await _send(ctx, "/upb all", REGISTRAR, 2)
    assert (await _texts(transport))[-1] == "pong"


async def test_ping_rate_limit_per_chat_and_user_and_root_exempt(db):
    ctx, services, transport, clock = mk_ctx(db, cooldown=5.0)
    await _world(db, services, active=True)
    await register_chat(db, services, 501, REGISTRAR)
    await subscribe(db, services, 501, SUB)
    await subscribe(db, services, 501, 21)

    await _send(ctx, "/upb all", SUB, 1)
    await _send(ctx, "/upb all", SUB, 2)  # inside the window: silently ignored
    assert len(transport.calls) == 1
    assert await _count(db, "SELECT outcome FROM processed_updates WHERE update_id = 2") == "ignored"

    await _send(ctx, "/upb all", 21, 3)  # another user
    await _send(ctx, "/upb all", SUB, 4, chat_id=501)  # another chat
    assert len(transport.calls) == 3

    clock.advance(5)
    await _send(ctx, "/upb all", SUB, 5)
    assert len(transport.calls) == 4

    for i in (6, 7, 8):
        await _send(ctx, "/upb all", ROOT, i)
    assert len(transport.calls) == 7  # root has no limit


async def test_denied_ping_does_not_start_a_cooldown(db):
    ctx, services, transport, _c = mk_ctx(db, cooldown=5.0)
    await _world(db, services, active=True)
    await _send(ctx, "/upb all", NOBODY, 1)
    await _send(ctx, "/upb notify on", NOBODY, 2)
    await _send(ctx, "/upb all", NOBODY, 3)
    assert len(transport.calls) == 2  # the subscribe answer and the ping


# --- list ---


async def test_list_shows_everyone_including_the_initiator_without_at_signs(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb list", SUB, 1)
    text = transport.calls[0]["text"]
    assert "User20 (user20) - 20" in text and "U21 - 21" in text
    assert "@" not in text and "tg://" not in text


async def test_empty_list_uses_the_list_empty_phrase(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, REGISTRAR)
    await register_chat(db, services, CHAT, REGISTRAR)
    await _send(ctx, "/upb list", REGISTRAR, 1)
    assert await _texts(transport) == ["No subscribers yet."]


# --- help / usage ---


async def test_help_lists_only_what_the_author_may_run(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb help", SUB, 1)
    await _send(ctx, "/upb usage", REGISTRAR, 2)
    sub_text, owner_text = await _texts(transport)
    assert "/all \u2014 Ping all" in sub_text and "/unregister" not in sub_text
    assert "/unregister \u2014 Unregister" in owner_text and "/lang &lt;en|ru&gt; \u2014 Language" in owner_text
    assert "/upb" not in sub_text + owner_text and "Recipients are fixed" not in sub_text


@pytest.mark.parametrize(
    "who,text,expected",
    [
        ("nobody", "/upb", ["/on"]),
        ("nobody", "/upb qwe", ["/on"]),
        ("nobody", "/upb notify", ["/on"]),
        ("subscriber", "/upb notify", ["/on", "/off", "/list"]),
        ("owner", "/upb chat", ["/unregister"]),
        ("owner", "/upb lang", ["/lang &lt;en|ru&gt;"]),
        ("owner", "/lang", ["/lang &lt;en|ru&gt;"]),
    ],
)
async def test_partial_help_active_chat(db, who, text, expected):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, text, WHO[who], 1)
    out = transport.calls[0]["text"]
    for syntax in expected:
        assert syntax in out
    if who == "nobody":
        assert "/all" not in out and "/list" not in out
    assert "/register" not in out  # already registered: not advertised


@pytest.mark.parametrize("who", ["root", "owner", "subscriber", "nobody"])
@pytest.mark.parametrize("text", ["/upb", "/upb help", "/upb chat", "/upb qwe"])
async def test_register_is_not_advertised_in_an_active_chat_but_still_runs(db, who, text):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, text, WHO[who], 1)
    assert all("/register" not in c["text"] for c in transport.calls)
    await _send(ctx, "/upb chat register", ROOT, 2)
    assert (await _texts(transport))[-1] == t("already_registered", "en")


async def test_register_is_listed_in_an_inactive_chat_for_staff(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=False)
    await _send(ctx, "/upb", FOREIGN_ADMIN, 1)
    assert "/register \u2014 Register" in transport.calls[0]["text"]


@pytest.mark.parametrize("who,text", [("subscriber", "/upb"), ("nobody", "/upb notify"), ("nobody", "/upb chat")])
async def test_partial_help_inactive_chat_is_for_staff_only(db, who, text):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=False)
    await _send(ctx, text, WHO[who], 1)
    assert transport.calls == []


async def test_partial_help_inactive_chat_shows_only_register_to_an_admin(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=False)
    await _send(ctx, "/upb", FOREIGN_ADMIN, 1)
    out = transport.calls[0]["text"]
    assert "/register" in out and "/on" not in out


async def test_usage_with_nothing_to_show_is_silent(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    # a plain user knows the prefix "chat" but owns nothing under it
    await _send(ctx, "/upb chat", NOBODY, 1)
    await _send(ctx, "/upb lang", SUB, 2)
    assert transport.calls == []


async def test_a_typo_in_the_command_name_is_somebody_elses_command(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/ubb help", SUB, 1)
    await _send(ctx, "/upb@other_bot help", SUB, 2)
    await _send(ctx, "just text", SUB, 3)
    assert transport.calls == []
    assert await _count(db, "SELECT COUNT(*) FROM processed_updates") == 0  # not recognised: not recorded


# --- lang ---


async def test_lang_sets_the_chat_language_and_replies_in_it(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb lang ru", REGISTRAR, 1)
    await _send(ctx, "/upb notify on", NOBODY, 2)
    await _send(ctx, "/upb list", SUB, 3)
    await _send(ctx, "/upb LANG EN", ROOT, 4)
    await _send(ctx, "/upb notify on", 41, 5)
    texts = await _texts(transport)
    assert texts[0] == "\u042f\u0437\u044b\u043a: \u0440\u0443\u0441\u0441\u043a\u0438\u0439."
    assert texts[1] == "\u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0430."
    assert texts[2].startswith("User20") or "20" in texts[2]
    assert texts[3] == "Language: English."
    assert texts[4] == "Subscribed."


async def test_lang_bad_argument_gets_a_syntax_hint_only_for_owners(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb lang de", REGISTRAR, 1)
    await _send(ctx, "/upb lang de", SUB, 2)
    await _send(ctx, "/upb lang ru extra", REGISTRAR, 3)
    assert await _texts(transport) == [
        "Invalid arguments. Usage: <code>/upb lang &lt;en|ru&gt;</code>",
        "Invalid arguments. Usage: <code>/upb lang &lt;en|ru&gt;</code>",
    ]
    async with db.reader() as c:
        assert (await services.get_chat(c, CHAT)).lang == "en"


# --- delivery details, robustness ---


async def test_replies_reply_to_the_command_and_stay_in_the_topic(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, "/upb notify on", NOBODY, 1, message_id=55, thread_id=12)
    await _send(ctx, "/upb help", SUB, 2, message_id=56)
    assert [(c["reply_to_message_id"], c["thread_id"]) for c in transport.calls] == [(55, 12), (56, None)]


async def test_duplicate_update_is_a_no_op(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    ev = group_event("/upb notify on", update_id=1, user_id=NOBODY)
    await handle_event(ctx, ev)
    await handle_event(ctx, ev)
    assert len(transport.calls) == 1


async def test_commands_from_an_already_migrated_group_id_are_ignored(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    async with db.transaction() as c:
        await services.migrate_chat(c, CHAT, -1001)
    await _send(ctx, "/upb notify on", NOBODY, 1)
    await _send(ctx, "/upb chat register", REGISTRAR, 2)
    assert transport.calls == []
    assert await _count(db, "SELECT COUNT(*) FROM chats WHERE chat_id = ?", CHAT) == 0
    assert await _count(db, "SELECT COUNT(*) FROM processed_updates") == 2
    await _send(ctx, "/upb list", SUB, 3, chat_id=-1001)  # the new id works
    assert len(transport.calls) == 1


async def test_bots_anonymous_and_edited_messages_do_nothing(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await handle_event(ctx, group_event("/upb notify on", update_id=1, user_id=NOBODY, is_bot=True))
    await handle_event(ctx, group_event("/upb notify on", update_id=2, user_id=None))
    await handle_event(ctx, group_event("/upb notify on", update_id=3, user_id=NOBODY, edited=True))
    assert transport.calls == []


@pytest.mark.parametrize("exc", [PermanentSend(), AmbiguousSend(), RateLimited(0.0)])
async def test_a_failing_reply_never_escapes_and_keeps_the_state(db, exc):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    transport.queue_raises([exc] * 10)
    await _send(ctx, "/upb notify on", NOBODY, 1)
    await _send(ctx, "/upb all", REGISTRAR, 2)
    await _send(ctx, "/upb list", REGISTRAR, 3)
    async with db.reader() as c:
        assert await services.is_subscribed(c, CHAT, NOBODY)
    assert await _count(db, "SELECT COUNT(*) FROM processed_updates WHERE outcome = 'ok'") == 3


async def test_poison_update_is_recorded_as_error_and_processing_continues(db, monkeypatch):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)

    async def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(services, "subscribe", boom)
    await _send(ctx, "/upb notify on", NOBODY, 1)
    monkeypatch.undo()
    assert transport.calls == []
    assert await _count(db, "SELECT outcome FROM processed_updates WHERE update_id = 1") == "error"
    async with db.reader() as c:
        assert not await services.is_subscribed(c, CHAT, NOBODY)  # rolled back
    await _send(ctx, "/upb notify on", NOBODY, 2)
    assert await _texts(transport) == ["Subscribed."]


async def test_error_log_carries_the_class_only(db, monkeypatch, caplog):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)

    async def boom(*a, **k):
        raise RuntimeError("FAKE-TOKEN-MARKER")

    monkeypatch.setattr(services, "subscribe", boom)
    await _send(ctx, "/upb notify on", NOBODY, 1)
    assert "RuntimeError" in caplog.text
    assert "FAKE-TOKEN-MARKER" not in caplog.text


@pytest.mark.parametrize("text", ["/help", "/usage", "/upb help", "/upb usage", "/help@upb_bot"])
async def test_staff_help_in_a_free_chat_lists_register_and_help(db, text):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=False)
    await _send(ctx, text, FOREIGN_ADMIN, 1)
    (reply,) = await _texts(transport)
    assert reply == rendering.help_text(
        Actor(user_id=FOREIGN_ADMIN, role=Role.ADMIN),
        scope=Scope.GROUP, chat_active=False, lang="en",
    )
    assert "/register" in reply and "/help" in reply


async def test_non_staff_help_in_a_free_chat_makes_no_calls(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=False)
    await _send(ctx, "/help", SUB, 1)
    assert transport.calls == [] and transport.menu_calls == []


@pytest.mark.parametrize(
    "text,syntax",
    [
        ("/lang de", "/lang <en|ru>"),
        ("/LANG de", "/lang <en|ru>"),
        ("/upb lang de", "/upb lang <en|ru>"),
    ],
)
async def test_bad_lang_args_show_the_syntax_that_was_used(db, text, syntax):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services, active=True)
    await _send(ctx, text, ROOT, 1)
    assert await _texts(transport) == [t("bad_args", "en", syntax=syntax)]
    assert "&lt;en|ru&gt;" in (await _texts(transport))[0]  # still escaped
