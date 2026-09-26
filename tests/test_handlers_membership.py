import pytest

from app.delivery import Delivery
from app.handlers import Context, handle_event
from app.services import Services

from conftest import FakeClock, RecordingTransport, make_event

BOT_ID = 999
BOT_USERNAME = "upb_bot"
REGISTRAR = 1


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


async def _register_chat(db, services, chat_id, registrar_id=REGISTRAR):
    async with db.transaction() as c:
        await services.touch_user(c, registrar_id)
        return await services.register_chat(c, chat_id, "Chat", registrar_id)


async def _subscribe(db, services, chat_id, user_id):
    async with db.transaction() as c:
        await services.touch_user(c, user_id, display_name=f"U{user_id}")
        return await services.subscribe(c, chat_id, user_id)


def _my_chat_member_event(*, update_id, chat_id, bot_removed):
    return make_event(
        kind="my_chat_member",
        chat_type="group",
        chat_id=chat_id,
        update_id=update_id,
        user_id=None,
        text=None,
        bot_removed=bot_removed,
    )


def _chat_member_event(*, update_id, chat_id, left_user_id):
    return make_event(
        kind="chat_member",
        chat_type="group",
        chat_id=chat_id,
        update_id=update_id,
        user_id=None,
        text=None,
        left_user_id=left_user_id,
    )


def _member_left_event(*, update_id, chat_id, left_user_id):
    return make_event(
        kind="member_left",
        chat_type="group",
        chat_id=chat_id,
        update_id=update_id,
        user_id=None,
        text=None,
        left_user_id=left_user_id,
    )


def _migration_event(*, update_id, chat_id, migrate_to=None, migrate_from=None):
    return make_event(
        kind="message",
        chat_type="supergroup" if migrate_from else "group",
        chat_id=chat_id,
        update_id=update_id,
        user_id=None,
        text=None,
        migrate_to_chat_id=migrate_to,
        migrate_from_chat_id=migrate_from,
    )


# --- bot removed / re-added ---


@pytest.mark.asyncio
async def test_bot_removed_unregisters_and_clears_subscriptions_no_transport_calls(db):
    ctx, services, transport, clock = _mk_ctx(db)
    CHAT = 900
    await _register_chat(db, services, CHAT)
    await _subscribe(db, services, CHAT, 9001)

    await handle_event(ctx, _my_chat_member_event(update_id=1, chat_id=CHAT, bot_removed=True))

    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert await services.list_subscribers(c, CHAT) == []
    assert transport.calls == []  # never tries to message an unreachable chat


@pytest.mark.asyncio
async def test_bot_readded_restores_nothing_stays_silent(db):
    ctx, services, transport, clock = _mk_ctx(db)
    CHAT = 901
    await _register_chat(db, services, CHAT)
    await _subscribe(db, services, CHAT, 9002)
    await handle_event(ctx, _my_chat_member_event(update_id=1, chat_id=CHAT, bot_removed=True))

    await handle_event(ctx, _my_chat_member_event(update_id=2, chat_id=CHAT, bot_removed=False))

    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert await services.list_subscribers(c, CHAT) == []
    assert transport.calls == []


# --- ordinary members leaving ---


@pytest.mark.asyncio
async def test_member_left_service_message_clears_only_that_subscription(db):
    ctx, services, transport, clock = _mk_ctx(db)
    CHAT = 902
    await _register_chat(db, services, CHAT)
    await _subscribe(db, services, CHAT, 9101)
    await _subscribe(db, services, CHAT, 9102)

    await handle_event(ctx, _member_left_event(update_id=1, chat_id=CHAT, left_user_id=9101))

    async with db.reader() as c:
        assert await services.subscription_id_of(c, CHAT, 9101) is None
        assert await services.subscription_id_of(c, CHAT, 9102) is not None
        assert await services.get_chat(c, CHAT) is not None  # chat itself stays registered


@pytest.mark.asyncio
async def test_chat_member_leaving_clears_subscription(db):
    ctx, services, transport, clock = _mk_ctx(db)
    CHAT = 903
    await _register_chat(db, services, CHAT)
    await _subscribe(db, services, CHAT, 9201)

    await handle_event(ctx, _chat_member_event(update_id=1, chat_id=CHAT, left_user_id=9201))

    async with db.reader() as c:
        assert await services.subscription_id_of(c, CHAT, 9201) is None


@pytest.mark.asyncio
async def test_chat_member_restriction_change_does_not_unsubscribe(db):
    ctx, services, transport, clock = _mk_ctx(db)
    CHAT = 904
    await _register_chat(db, services, CHAT)
    await _subscribe(db, services, CHAT, 9301)

    # a restriction change on a remaining member carries no left_user_id
    await handle_event(ctx, _chat_member_event(update_id=1, chat_id=CHAT, left_user_id=None))

    async with db.reader() as c:
        assert await services.subscription_id_of(c, CHAT, 9301) is not None


# --- migration ordering (plan section 10) ---


@pytest.mark.asyncio
async def test_migrate_then_old_id_leave_keeps_migrated_state(db):
    OLD, NEW = 910, 911
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, OLD)
    await _subscribe(db, services, OLD, 9401)

    await handle_event(ctx, _migration_event(update_id=1, chat_id=OLD, migrate_to=NEW))

    # restart: fresh Context/Services/Delivery over the same database
    ctx2, services2, transport2, clock2 = _mk_ctx(db)

    # a stale "bot left the old chat" event arrives after the migration
    await handle_event(ctx2, _my_chat_member_event(update_id=2, chat_id=OLD, bot_removed=True))

    async with db.reader() as c:
        assert await services2.resolve_chat_id(c, OLD) == NEW
        new_chat = await services2.get_chat(c, NEW)
        assert new_chat is not None
        assert await services2.subscription_id_of(c, NEW, 9401) is not None


@pytest.mark.asyncio
async def test_old_id_leave_then_migrate_leaves_new_chat_inactive(db):
    OLD, NEW = 920, 921
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, OLD)
    await _subscribe(db, services, OLD, 9501)

    await handle_event(ctx, _my_chat_member_event(update_id=1, chat_id=OLD, bot_removed=True))

    async with db.reader() as c:
        assert await services.get_chat(c, OLD) is None

    # restart
    ctx2, services2, transport2, clock2 = _mk_ctx(db)

    await handle_event(ctx2, _migration_event(update_id=2, chat_id=OLD, migrate_to=NEW))

    async with db.reader() as c:
        assert await services2.resolve_chat_id(c, OLD) == NEW  # alias still recorded
        assert await services2.get_chat(c, NEW) is None  # registration NOT resurrected
        assert await services2.list_subscribers(c, NEW) == []


@pytest.mark.asyncio
async def test_repeated_migration_pair_is_a_noop(db):
    OLD, NEW = 930, 931
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, OLD)
    await _subscribe(db, services, OLD, 9601)

    # two real updates for the same pair: one seen in the old chat, one in the new
    await handle_event(ctx, _migration_event(update_id=1, chat_id=OLD, migrate_to=NEW))
    await handle_event(ctx, _migration_event(update_id=2, chat_id=NEW, migrate_from=OLD))

    async with db.reader() as c:
        assert await services.list_conflicts(c) == []
        assert await services.resolve_chat_id(c, OLD) == NEW
        assert await services.subscription_id_of(c, NEW, 9601) is not None
        subs = await services.list_subscribers(c, NEW)
    assert len(subs) == 1  # not moved/duplicated twice


@pytest.mark.asyncio
async def test_leave_then_readd_without_migration_does_not_restore(db):
    CHAT = 940
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT)
    await _subscribe(db, services, CHAT, 9701)

    await handle_event(ctx, _my_chat_member_event(update_id=1, chat_id=CHAT, bot_removed=True))

    ctx2, services2, transport2, clock2 = _mk_ctx(db)
    await handle_event(ctx2, _my_chat_member_event(update_id=2, chat_id=CHAT, bot_removed=False))

    async with db.reader() as c:
        assert await services2.get_chat(c, CHAT) is None
        assert await services2.list_subscribers(c, CHAT) == []


# --- dedup ---


@pytest.mark.asyncio
async def test_replayed_bot_removal_update_does_not_duplicate_effect(db):
    CHAT = 950
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT)
    event = _my_chat_member_event(update_id=1, chat_id=CHAT, bot_removed=True)

    await handle_event(ctx, event)
    await handle_event(ctx, event)  # exact replay

    async with db.reader() as c:
        due = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    # farewell=False path: nothing queued either time, replay or not
    assert due == []
