import pytest

from app.delivery import AmbiguousSend, PermanentSend
from app.handlers import handle_event

from conftest import (
    make_admin,
    make_root,
    mk_ctx,
    private_event,
    register_chat,
    subscribe,
)

ROOT = 10
ADMIN = 1
ADMIN2 = 2
SUB = 20
NOBODY = 40

WHO = {"root": ROOT, "admin": ADMIN, "subscriber": SUB, "nobody": NOBODY}

# (text, who may run it)
COMMANDS = {
    "/help": {"root", "admin"},
    "/usage": {"root", "admin"},
    "/start": {"root", "admin"},
    "/lang ru": {"root", "admin"},
    "/chat list": {"root", "admin"},
    "/chat remove 500": {"root"},
    "/admin create 77": {"root"},
    "/admin remove 2": {"root"},
    "/admin list": {"root"},
    "/admin": {"root"},
    "/chat": {"root", "admin"},
}


async def _world(db, services):
    await make_root(db, services, ROOT)
    await make_admin(db, services, ADMIN)
    await make_admin(db, services, ADMIN2)
    await register_chat(db, services, 500, ADMIN, "Alpha")
    await register_chat(db, services, 501, ADMIN2, "Beta")
    await subscribe(db, services, 500, SUB)


async def _send(ctx, text, user, uid, **kw):
    await handle_event(ctx, private_event(text, update_id=uid, user_id=user, **kw))


def _texts(transport):
    return [c["text"] for c in transport.calls]


async def _one(db, sql, *params):
    async with db.reader() as c:
        cur = await c.execute(sql, params)
        row = await cur.fetchone()
        return row[0] if row else None


@pytest.mark.parametrize("who", list(WHO))
@pytest.mark.parametrize("text", list(COMMANDS))
async def test_private_rights_matrix(db, text, who):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, text, WHO[who], 1)
    if who in COMMANDS[text]:
        assert len(transport.calls) >= 1
    else:
        assert transport.calls == []
        assert await _one(db, "SELECT COUNT(*) FROM outbox") == 0
        assert await _one(db, "SELECT COUNT(*) FROM chats") == 2
        assert await _one(db, "SELECT COUNT(*) FROM roles") == 3
    # the contact is recorded and the update claimed on every path
    assert await _one(db, "SELECT private_contact_at IS NOT NULL FROM users WHERE user_id = ?", WHO[who]) == 1
    assert await _one(db, "SELECT COUNT(*) FROM processed_updates WHERE update_id = 1") == 1


async def test_plain_text_and_unknown_commands_only_record_the_contact(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "hello", ROOT, 1)
    await _send(ctx, "/whoami", ROOT, 2)
    await _send(ctx, "/upb all", ROOT, 3)
    assert transport.calls == []
    assert await _one(db, "SELECT COUNT(*) FROM processed_updates WHERE outcome = 'ignored'") == 3


async def test_help_by_role_with_registration_hint(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/help", ADMIN, 1)
    await _send(ctx, "/start", ROOT, 2)
    admin_text, root_text = _texts(transport)
    assert "/chat list" in admin_text and "/admin create" not in admin_text
    assert "/admin create" in root_text and "/chat remove" in root_text
    assert "/upb chat register" in admin_text


async def test_lang_persists_per_user_and_replies_in_it(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/lang ru", ADMIN, 1)
    await _send(ctx, "/chat list", ADMIN, 2)
    await _send(ctx, "/admin list", ROOT, 3)  # root's language is still en
    await _send(ctx, "/lang EN", ADMIN, 4)
    await _send(ctx, "/lang xx", ADMIN, 5)
    texts = _texts(transport)
    assert texts[0] == "\u042f\u0437\u044b\u043a: \u0440\u0443\u0441\u0441\u043a\u0438\u0439."
    assert texts[1] == "Alpha | 500"
    assert texts[2] == "User1 (user1) - 1\nname unknown - 2"  # root list is in en
    assert texts[3] == "Language: English."
    assert texts[4] == "Invalid arguments. Usage: <code>/lang &lt;en|ru&gt;</code>"


async def test_bad_args_reply_is_in_the_user_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/lang ru", ROOT, 1)
    await _send(ctx, "/admin create abc", ROOT, 2)
    await _send(ctx, "/chat remove", ROOT, 3)
    await _send(ctx, "/admin create abc", ADMIN, 4)  # no right: silence
    assert _texts(transport)[1:] == [
        "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0435 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u044b. \u0424\u043e\u0440\u043c\u0430\u0442: <code>/admin create &lt;user_id&gt;</code>",
        "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0435 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u044b. \u0424\u043e\u0440\u043c\u0430\u0442: <code>/chat remove &lt;chat_id&gt;</code>",
    ]


async def test_chat_list_admin_sees_own_chats_root_sees_all_with_registrar(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/chat list", ADMIN, 1)
    await _send(ctx, "/chat list", ROOT, 2)
    admin_text, root_text = _texts(transport)
    assert admin_text == "Alpha | 500"
    assert root_text == "Alpha | 500 | 1\nBeta | 501 | 2"


async def test_chat_list_empty(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, ADMIN)
    await _send(ctx, "/chat list", ADMIN, 1)
    assert _texts(transport) == ["No registered chats."]


async def test_chat_remove_removes_only_that_chat_and_keeps_the_role(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, 502, ADMIN, "Gamma")
    await _send(ctx, "/chat remove 500", ROOT, 1)
    assert _texts(transport) == ["Chat unregistered: 500."]
    async with db.reader() as c:
        assert await services.get_chat(c, 500) is None
        assert await services.get_chat(c, 502) is not None
        assert await services.get_role(c, ADMIN) is not None
        assert not await services.is_subscribed(c, 500, SUB)
    assert await _one(db, "SELECT COUNT(*) FROM outbox WHERE event_type = 'chat_farewell'") == 1


async def test_chat_remove_accepts_the_old_id_of_a_migrated_group(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.migrate_chat(c, 500, -1000500)
    await _send(ctx, "/chat remove 500", ROOT, 1)
    assert _texts(transport) == ["Chat unregistered: -1000500."]


async def test_chat_remove_of_an_unknown_chat_says_so(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/chat remove 9", ROOT, 1)
    assert _texts(transport) == ["No active registration: 9."]


async def test_admin_create_exists_and_root_guard(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/admin create 77", ROOT, 1)
    await _send(ctx, "/admin create 77", ROOT, 2)
    await _send(ctx, f"/admin create {ROOT}", ROOT, 3)
    await _send(ctx, f"/admin remove {ROOT}", ROOT, 4)
    assert _texts(transport) == [
        "Admin granted: 77.",
        "Already admin: 77.",
        "Root is assigned via CLI only.",
        "Root is assigned via CLI only.",
    ]
    async with db.reader() as c:
        assert (await services.get_root(c)) == ROOT


async def test_admin_remove_cascades_to_his_chats_only(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/admin remove 1", ROOT, 1)
    await _send(ctx, "/admin remove 1", ROOT, 2)
    assert _texts(transport) == ["Admin revoked: 1. Chats removed: 1.", "Not an admin: 1."]
    async with db.reader() as c:
        assert await services.get_chat(c, 500) is None
        assert await services.get_chat(c, 501) is not None
    assert await _one(db, "SELECT COUNT(*) FROM outbox WHERE target_id = 500") == 1


async def test_admin_list_and_empty(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    await _send(ctx, "/admin list", ROOT, 1)
    await make_admin(db, services, ADMIN)
    async with db.transaction() as c:
        await services.touch_user(c, ADMIN, display_name="Ann", username="ann")
    await _send(ctx, "/admin list", ROOT, 2)
    assert _texts(transport) == ["No admins.", "Ann (ann) - 1"]


async def test_partial_private_help(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/admin", ROOT, 1)
    await _send(ctx, "/admin xyz", ROOT, 2)
    await _send(ctx, "/admin", ADMIN, 3)  # nothing under /admin for an admin: silence
    await _send(ctx, "/chat", ADMIN, 4)
    await _send(ctx, "/chat", ROOT, 5)
    await _send(ctx, "/lang", ADMIN, 6)
    t = _texts(transport)
    assert len(t) == 5
    assert "/admin create" in t[0] and "/chat list" not in t[0]
    assert t[0] == t[1]
    assert "/chat list" in t[2] and "/chat remove" not in t[2]
    assert "/chat remove" in t[3]
    assert "/lang" in t[4]


async def test_subscription_gives_no_private_access(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/help", SUB, 1)
    await _send(ctx, "/chat list", SUB, 2)
    assert transport.calls == []


@pytest.mark.parametrize("exc", [PermanentSend(), AmbiguousSend()])
async def test_a_failing_reply_never_escapes(db, exc):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    transport.queue_raises([exc] * 5)
    await _send(ctx, "/admin create 77", ROOT, 1)
    async with db.reader() as c:
        assert (await services.get_role(c, 77)) is not None  # the mutation stands


async def test_private_reply_targets_the_command_message(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _send(ctx, "/help", ROOT, 1, message_id=33)
    assert transport.calls[0]["chat_id"] == ROOT
    assert transport.calls[0]["reply_to_message_id"] == 33


async def test_poison_private_update_is_recorded_as_error(db, monkeypatch):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)

    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(services, "grant_admin", boom)
    await _send(ctx, "/admin create 77", ROOT, 1)
    monkeypatch.undo()
    assert await _one(db, "SELECT outcome FROM processed_updates WHERE update_id = 1") == "error"
    await _send(ctx, "/admin create 77", ROOT, 2)
    assert _texts(transport) == ["Admin granted: 77."]
