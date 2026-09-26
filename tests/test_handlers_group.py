import pytest

from app import access
from app.delivery import Delivery
from app.handlers import Context, handle_event
from app.models import Cmd, Scope
from app.services import Services

from conftest import FakeClock, RecordingTransport, make_event

BOT_ID = 999
BOT_USERNAME = "upb_bot"
CHAT = 500
OTHER_CHAT = 501
REGISTRAR = 1
ACTOR = 2000

GROUP_TEXTS = {
    Cmd.CHAT_REGISTER: "/upb chat register",
    Cmd.CHAT_UNREGISTER: "/upb chat unregister",
    Cmd.NOTIFY_ON: "/upb notify on",
    Cmd.NOTIFY_OFF: "/upb notify off",
    Cmd.PING: "/upb all",
    Cmd.LIST: "/upb list",
    Cmd.HELP: "/upb help",
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


def _group_event(cmd_or_text, *, update_id, user_id, chat_id=CHAT, message_id=None, **kwargs):
    text = GROUP_TEXTS.get(cmd_or_text, cmd_or_text)
    defaults = dict(
        kind="message",
        chat_type="group",
        chat_id=chat_id,
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


# --- section 15 matrix: every command x role x active/inactive, zero calls when denied ---

ROLE_SETUPS = ["root", "admin", "subscriber_here", "subscriber_elsewhere", "none"]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_active", [True, False])
@pytest.mark.parametrize("role", ROLE_SETUPS)
@pytest.mark.parametrize("cmd", list(GROUP_TEXTS))
async def test_group_rights_matrix_silence_when_denied(db, cmd, role, chat_active):
    ctx, services, transport, clock = _mk_ctx(db)

    if chat_active:
        await _register_chat(db, services, CHAT, REGISTRAR)

    if role == "root":
        await _make_root(db, services, ACTOR)
    elif role == "admin":
        await _make_admin(db, services, ACTOR)
    elif role == "subscriber_here":
        async with db.transaction() as c:
            await services.touch_user(c, ACTOR)
        if chat_active:
            await _subscribe(db, services, CHAT, ACTOR)
    elif role == "subscriber_elsewhere":
        await _register_chat(db, services, OTHER_CHAT, REGISTRAR)
        await _subscribe(db, services, OTHER_CHAT, ACTOR)
    # "none": nothing set up

    async with db.reader() as c:
        actor = await services.load_actor(c, ACTOR, chat_id=CHAT)
    expected_ok = access.can_run(cmd, actor, scope=Scope.GROUP, chat_active=chat_active)

    event = _group_event(cmd, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)

    if expected_ok:
        return  # happy paths are covered by the dedicated tests below
    assert transport.calls == []


@pytest.mark.asyncio
async def test_anonymous_admin_or_channel_post_never_registers(db):
    ctx, services, transport, clock = _mk_ctx(db)
    text = GROUP_TEXTS[Cmd.CHAT_REGISTER]
    event = make_event(
        kind="message",
        chat_type="group",
        chat_id=CHAT,
        update_id=1,
        user_id=None,  # sender_chat / anonymous admin: no identified user
        message_id=1,
        text=text,
        entities=_entities(text),
    )
    await handle_event(ctx, event)
    assert transport.calls == []
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None


@pytest.mark.asyncio
async def test_bot_message_is_ignored(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    event = _group_event(Cmd.PING, update_id=1, user_id=999999, is_bot=True)
    await handle_event(ctx, event)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_edited_message_never_reruns_a_command(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    event = _group_event(Cmd.CHAT_REGISTER, update_id=1, user_id=ACTOR, edited=True)
    await handle_event(ctx, event)
    assert transport.calls == []
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None


@pytest.mark.asyncio
async def test_unknown_command_in_group_is_silent(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    text = "/upb bogus"
    event = _group_event(text, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)
    assert transport.calls == []


# --- happy paths ---


@pytest.mark.asyncio
async def test_register_by_staff_creates_chat_and_welcomes(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    event = _group_event(Cmd.CHAT_REGISTER, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)

    async with db.reader() as c:
        chat = await services.get_chat(c, CHAT)
    assert chat is not None
    assert chat.registered_by == ACTOR
    assert len(transport.calls) == 1
    assert "registered" in transport.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_register_idempotent_keeps_registrar_and_generation(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)
    await _make_admin(db, services, 3001)

    event1 = _group_event(Cmd.CHAT_REGISTER, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event1)
    async with db.reader() as c:
        chat_before = await services.get_chat(c, CHAT)

    event2 = _group_event(Cmd.CHAT_REGISTER, update_id=2, user_id=3001)
    await handle_event(ctx, event2)
    async with db.reader() as c:
        chat_after = await services.get_chat(c, CHAT)

    assert chat_after.registered_by == chat_before.registered_by == ACTOR
    assert chat_after.registration_generation == chat_before.registration_generation
    assert len(transport.calls) == 2
    assert "already registered" in transport.calls[1]["text"].lower()


@pytest.mark.asyncio
async def test_unregister_clears_subscriptions_and_queues_farewell(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _make_root(db, services, ACTOR)
    await _subscribe(db, services, CHAT, 7001)

    event = _group_event(Cmd.CHAT_UNREGISTER, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)

    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        subs = await services.list_subscribers(c, CHAT)
        assert subs == []
        due = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert any(e.event_type == "chat_farewell" and e.target_id == CHAT for e in due)
    # farewell is delivered via the outbox loop, not synchronously here
    assert transport.calls == []


@pytest.mark.asyncio
async def test_notify_on_anyone_in_active_chat_subscribes(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    event = _group_event(Cmd.NOTIFY_ON, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)

    async with db.reader() as c:
        sid = await services.subscription_id_of(c, CHAT, ACTOR)
    assert sid is not None
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_notify_on_repeated_does_not_duplicate(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await handle_event(ctx, _group_event(Cmd.NOTIFY_ON, update_id=1, user_id=ACTOR))
    async with db.reader() as c:
        sid1 = await services.subscription_id_of(c, CHAT, ACTOR)
    await handle_event(ctx, _group_event(Cmd.NOTIFY_ON, update_id=2, user_id=ACTOR))
    async with db.reader() as c:
        sid2 = await services.subscription_id_of(c, CHAT, ACTOR)
    assert sid1 == sid2


@pytest.mark.asyncio
async def test_notify_off_by_subscriber_removes_and_confirms_once(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _subscribe(db, services, CHAT, ACTOR)

    event = _group_event(Cmd.NOTIFY_OFF, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)

    async with db.reader() as c:
        assert await services.subscription_id_of(c, CHAT, ACTOR) is None
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_notify_off_by_staff_without_subscription_still_gets_one_confirmation(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _make_admin(db, services, ACTOR)

    event = _group_event(Cmd.NOTIFY_OFF, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)

    assert len(transport.calls) == 1  # confirmation granted purely on authorization


@pytest.mark.asyncio
async def test_notify_off_repeated_by_plain_unsubscribed_user_is_silent(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _subscribe(db, services, CHAT, ACTOR)
    await handle_event(ctx, _group_event(Cmd.NOTIFY_OFF, update_id=1, user_id=ACTOR))
    assert len(transport.calls) == 1

    await handle_event(ctx, _group_event(Cmd.NOTIFY_OFF, update_id=2, user_id=ACTOR))
    assert len(transport.calls) == 1  # no second confirmation: no role, no reply


@pytest.mark.asyncio
async def test_ping_empty_replies_pong(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _make_admin(db, services, ACTOR)

    await handle_event(ctx, _group_event(Cmd.PING, update_id=1, user_id=ACTOR))

    assert len(transport.calls) == 1
    assert transport.calls[0]["text"] == "pong"


@pytest.mark.asyncio
async def test_ping_includes_the_author_if_subscribed(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _subscribe(db, services, CHAT, ACTOR)
    await _subscribe(db, services, CHAT, 7002)

    await handle_event(ctx, _group_event(Cmd.PING, update_id=1, user_id=ACTOR))

    assert len(transport.calls) == 1
    text = transport.calls[0]["text"]
    assert f"id={ACTOR}" in text
    assert "id=7002" in text


@pytest.mark.asyncio
async def test_list_empty_replies_pong(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _make_root(db, services, ACTOR)

    await handle_event(ctx, _group_event(Cmd.LIST, update_id=1, user_id=ACTOR))

    assert len(transport.calls) == 1
    assert transport.calls[0]["text"] == "pong"


@pytest.mark.asyncio
async def test_list_shows_subscribers_without_mentions(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _subscribe(db, services, CHAT, ACTOR)

    await handle_event(ctx, _group_event(Cmd.LIST, update_id=1, user_id=ACTOR))

    assert len(transport.calls) == 1
    text = transport.calls[0]["text"]
    assert "tg://user" not in text
    assert str(ACTOR) in text


@pytest.mark.asyncio
async def test_help_reflects_allowed_commands_for_subscriber_vs_staff(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)
    await _subscribe(db, services, CHAT, ACTOR)
    await _make_admin(db, services, 3005)
    await _subscribe(db, services, CHAT, 3005)

    await handle_event(ctx, _group_event(Cmd.HELP, update_id=1, user_id=ACTOR))
    await handle_event(ctx, _group_event(Cmd.HELP, update_id=2, user_id=3005))

    subscriber_text = transport.calls[0]["text"]
    staff_text = transport.calls[1]["text"]
    assert "chat register" not in subscriber_text.lower()
    assert "chat register" in staff_text.lower()


# --- dedup ---


@pytest.mark.asyncio
async def test_replayed_update_does_not_duplicate_registration(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    event = _group_event(Cmd.CHAT_REGISTER, update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)
    await handle_event(ctx, event)  # exact replay, same update_id

    assert len(transport.calls) == 1  # no second welcome/no-op response


@pytest.mark.asyncio
async def test_replayed_forbidden_command_after_role_grant_still_does_nothing(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, REGISTRAR)

    denied_event = _group_event(Cmd.CHAT_UNREGISTER, update_id=1, user_id=ACTOR)
    await handle_event(ctx, denied_event)
    assert transport.calls == []
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is not None

    await _make_root(db, services, ACTOR)  # ACTOR is now root

    await handle_event(ctx, denied_event)  # same update_id replayed
    assert transport.calls == []
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is not None  # still registered: no re-run


@pytest.mark.asyncio
async def test_unregister_then_register_then_replay_of_unregister(db):
    ctx, services, transport, clock = _mk_ctx(db)
    await _make_root(db, services, ACTOR)

    await handle_event(ctx, _group_event(Cmd.CHAT_REGISTER, update_id=1, user_id=ACTOR))
    unregister_event = _group_event(Cmd.CHAT_UNREGISTER, update_id=2, user_id=ACTOR)
    await handle_event(ctx, unregister_event)
    await handle_event(ctx, _group_event(Cmd.CHAT_REGISTER, update_id=3, user_id=ACTOR))

    async with db.reader() as c:
        chat_before_replay = await services.get_chat(c, CHAT)
    assert chat_before_replay is not None

    await handle_event(ctx, unregister_event)  # replay of update_id=2

    async with db.reader() as c:
        chat_after_replay = await services.get_chat(c, CHAT)
    assert chat_after_replay is not None
    assert chat_after_replay.registration_generation == chat_before_replay.registration_generation
