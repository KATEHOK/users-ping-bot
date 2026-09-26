import pytest

from app import access
from app.delivery import Delivery
from app.handlers import Context, handle_event
from app.models import Cmd, Scope
from app.services import Services

from conftest import FakeClock, RecordingTransport, make_event

BOT_ID = 999
BOT_USERNAME = "upb_bot"
CHAT = 700  # a group chat used as a target for /chat remove
REGISTRAR = 1
ACTOR = 3000
TARGET_USER = 4000

PRIVATE_TEXTS = {
    Cmd.P_HELP: "/help",
    Cmd.ADMIN_CREATE: f"/admin create {TARGET_USER}",
    Cmd.ADMIN_REMOVE: f"/admin remove {TARGET_USER}",
    Cmd.ADMIN_LIST: "/admin list",
    Cmd.CHAT_LIST: "/chat list",
    Cmd.CHAT_REMOVE: f"/chat remove {CHAT}",
}


def _mk_ctx(db, clock=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db, services, transport, clock=clock)
    ctx = Context(
        db=db,
        services=services,
        delivery=delivery,
        bot_id=BOT_ID,
        bot_username=BOT_USERNAME,
        clock=clock,
    )
    return ctx, services, transport, clock


def _entities(text: str) -> tuple[tuple[str, int, int], ...]:
    first = text.split(" ", 1)[0]
    return (("bot_command", 0, len(first)),)


def _private_event(cmd_or_text, *, update_id, user_id, message_id=None, **kwargs):
    text = PRIVATE_TEXTS.get(cmd_or_text, cmd_or_text)
    defaults = dict(
        kind="message",
        chat_type="private",
        chat_id=user_id,
        update_id=update_id,
        user_id=user_id,
        username=f"user{user_id}",
        display_name=f"User{user_id}",
        message_id=message_id or update_id,
        text=text,
        entities=_entities(text),
    )
    defaults.update(kwargs)
    return make_event(**defaults)


async def _make_root(db, services, user_id):
    async with db.transaction() as c:
        await services.touch_user(c, user_id)
        await services.set_root(c, user_id)


async def _make_admin(db, services, user_id):
    async with db.transaction() as c:
        await services.touch_user(c, user_id)
        await services.grant_admin(c, user_id)


async def _register_chat(db, services, chat_id, registrar_id):
    async with db.transaction() as c:
        await services.touch_user(c, registrar_id)
        return await services.register_chat(c, chat_id, "Chat", registrar_id)


async def _subscribe(db, services, chat_id, user_id):
    async with db.transaction() as c:
        await services.touch_user(c, user_id, display_name=f"U{user_id}")
        return await services.subscribe(c, chat_id, user_id)


# --- section 15 matrix ---

ROLE_SETUPS = ["root", "admin", "subscriber", "none"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLE_SETUPS)
@pytest.mark.parametrize("cmd", list(PRIVATE_TEXTS))
async def test_private_rights_matrix_silence_when_denied(db, cmd, role):
    ctx, services, transport, clock = _mk_ctx(db)

    if role == "root":
        await _make_root(db, services, ACTOR)
    elif role == "admin":
        await _make_admin(db, services, ACTOR)
    elif role == "subscriber":
        await _register_chat(db, services, CHAT, REGISTRAR)
        await _subscribe(db, services, CHAT, ACTOR)
    # "none": nothing set up

    async with db.reader() as c:
        actor = await services.load_actor(c, ACTOR)
    expected_ok = access.can_run(cmd, actor, scope=Scope.PRIVATE, chat_active=True)

    await handle_event(ctx, _private_event(cmd, update_id=1, user_id=ACTOR))

    if expected_ok:
        return
    assert transport.calls == []


@pytest.mark.asyncio
async def test_admin_gets_silence_for_root_only_commands(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_admin(db, services, ACTOR)
    for i, cmd in enumerate((Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST, Cmd.CHAT_REMOVE)):
        await handle_event(ctx, _private_event(cmd, update_id=i + 1, user_id=ACTOR))
    assert transport.calls == []


@pytest.mark.asyncio
async def test_group_commands_do_not_work_in_private(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    text = "/upb list"
    event = _private_event(text, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_unknown_command_and_plain_text_are_silent_but_contact_recorded(db):
    ctx, services, transport, clock = _mk_ctx(db)
    event = _private_event("just chatting, no slash command", update_id=1, user_id=ACTOR, entities=())
    await handle_event(ctx, event)
    assert transport.calls == []

    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT private_contact_at FROM users WHERE user_id = ?", (ACTOR,)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] is not None


@pytest.mark.asyncio
async def test_start_shows_help_for_staff_and_silence_for_others(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await handle_event(ctx, _private_event("/start", update_id=1, user_id=ACTOR))
    assert len(transport.calls) == 1

    plain_user = 9999
    await handle_event(ctx, _private_event("/start", update_id=2, user_id=plain_user))
    assert len(transport.calls) == 1  # unchanged: no new send for the plain user


# --- happy paths and edge cases from plan section 5 ---


@pytest.mark.asyncio
async def test_admin_create_root_only_effect_no_side_effects(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    await handle_event(ctx, _private_event(Cmd.ADMIN_CREATE, update_id=1, user_id=ACTOR))
    async with db.reader() as c:
        role = await services.get_role(c, TARGET_USER)
    assert role is not None
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_admin_create_existing_admin_confirms_no_side_effects(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await _make_admin(db, services, TARGET_USER)

    await handle_event(ctx, _private_event(Cmd.ADMIN_CREATE, update_id=1, user_id=ACTOR))
    assert len(transport.calls) == 1
    assert "already an admin" in transport.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_admin_create_on_root_does_not_change_root(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    text = f"/admin create {ACTOR}"
    await handle_event(ctx, _private_event(text, update_id=1, user_id=ACTOR))

    async with db.reader() as c:
        root_id = await services.get_root(c)
    assert root_id == ACTOR  # unchanged
    assert len(transport.calls) == 1
    assert "cli" in transport.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_admin_remove_on_root_does_not_change_root(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    text = f"/admin remove {ACTOR}"
    await handle_event(ctx, _private_event(text, update_id=1, user_id=ACTOR))

    async with db.reader() as c:
        root_id = await services.get_root(c)
    assert root_id == ACTOR
    assert len(transport.calls) == 1
    assert "cli" in transport.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_admin_remove_nonexistent_admin_confirms_no_side_effects(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    await handle_event(ctx, _private_event(Cmd.ADMIN_REMOVE, update_id=1, user_id=ACTOR))
    assert len(transport.calls) == 1
    assert "not an admin" in transport.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_admin_remove_cascades_chats(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await _make_admin(db, services, TARGET_USER)
    await _register_chat(db, services, CHAT, TARGET_USER)
    await _subscribe(db, services, CHAT, 5001)

    await handle_event(ctx, _private_event(Cmd.ADMIN_REMOVE, update_id=1, user_id=ACTOR))

    async with db.reader() as c:
        assert await services.get_role(c, TARGET_USER) is None
        assert await services.get_chat(c, CHAT) is None
        assert await services.list_subscribers(c, CHAT) == []
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_admin_list_excludes_root_and_shows_unknown_name(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await _make_admin(db, services, TARGET_USER)  # granted by id, never contacted the bot

    await handle_event(ctx, _private_event(Cmd.ADMIN_LIST, update_id=1, user_id=ACTOR))

    text = transport.calls[0]["text"]
    assert str(ACTOR) not in text
    assert "name unknown" in text.lower()
    assert str(TARGET_USER) in text


@pytest.mark.asyncio
async def test_admin_list_empty_says_no_admins(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await handle_event(ctx, _private_event(Cmd.ADMIN_LIST, update_id=1, user_id=ACTOR))
    assert transport.calls[0]["text"] == "No admins."


@pytest.mark.asyncio
async def test_chat_list_has_no_links_or_usernames(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await _register_chat(db, services, CHAT, ACTOR)

    await handle_event(ctx, _private_event(Cmd.CHAT_LIST, update_id=1, user_id=ACTOR))

    text = transport.calls[0]["text"]
    assert "tg://" not in text
    assert "@" not in text
    assert "t.me" not in text
    assert str(CHAT) in text


@pytest.mark.asyncio
async def test_chat_remove_nonexistent_chat_confirms_no_side_effects(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    await handle_event(ctx, _private_event(Cmd.CHAT_REMOVE, update_id=1, user_id=ACTOR))
    assert len(transport.calls) == 1
    assert "no active registration" in transport.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_chat_remove_by_root_registrar_removes_only_that_chat(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    other_chat = CHAT + 1
    await _register_chat(db, services, CHAT, ACTOR)
    await _register_chat(db, services, other_chat, ACTOR)

    await handle_event(ctx, _private_event(Cmd.CHAT_REMOVE, update_id=1, user_id=ACTOR))

    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert await services.get_chat(c, other_chat) is not None
        assert await services.get_root(c) == ACTOR


@pytest.mark.asyncio
async def test_chat_remove_by_admin_registrar_cascades_admin_and_all_his_chats(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await _make_admin(db, services, TARGET_USER)
    other_chat = CHAT + 1
    await _register_chat(db, services, CHAT, TARGET_USER)
    await _register_chat(db, services, other_chat, TARGET_USER)

    await handle_event(ctx, _private_event(Cmd.CHAT_REMOVE, update_id=1, user_id=ACTOR))

    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert await services.get_chat(c, other_chat) is None
        assert await services.get_role(c, TARGET_USER) is None


@pytest.mark.asyncio
async def test_syntax_error_only_explained_to_authorized_user(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    text = "/admin create not-a-number"
    await handle_event(ctx, _private_event(text, update_id=1, user_id=ACTOR))

    assert len(transport.calls) == 1
    assert "invalid" in transport.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_syntax_error_from_unauthorized_user_is_silent(db):
    ctx, services, transport, clock = _mk_ctx(db)
    plain_user = 9999
    text = "/admin create not-a-number"
    await handle_event(ctx, _private_event(text, update_id=1, user_id=plain_user))
    assert transport.calls == []


# --- dedup ---


@pytest.mark.asyncio
async def test_replayed_update_does_not_duplicate_admin_grant(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    event = _private_event(Cmd.ADMIN_CREATE, update_id=1, user_id=ACTOR)

    await handle_event(ctx, event)
    await handle_event(ctx, event)

    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_replayed_forbidden_admin_command_after_role_grant_still_does_nothing(db):
    ctx, services, transport, clock = _mk_ctx(db)
    denied_event = _private_event(Cmd.ADMIN_CREATE, update_id=1, user_id=ACTOR)
    await handle_event(ctx, denied_event)
    assert transport.calls == []

    await _make_root(db, services, ACTOR)  # ACTOR is now root

    await handle_event(ctx, denied_event)  # same update_id
    assert transport.calls == []
    async with db.reader() as c:
        assert await services.get_role(c, TARGET_USER) is None  # never actually granted
