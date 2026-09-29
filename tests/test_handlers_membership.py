import logging

from app.handlers import handle_event

from conftest import (
    BOT_ID,
    group_event,
    make_admin,
    make_event,
    make_root,
    mk_ctx,
    private_event,
    register_chat,
    subscribe,
)

CHAT = 500
OTHER = 501
ADMIN = 1
ROOT = 10
SUB = 20
SUB2 = 21


async def _world(db, services):
    await make_root(db, services, ROOT)
    await make_admin(db, services, ADMIN)
    await register_chat(db, services, CHAT, ADMIN)
    await register_chat(db, services, OTHER, ADMIN)
    for chat in (CHAT, OTHER):
        await subscribe(db, services, chat, SUB)
    await subscribe(db, services, CHAT, SUB2)


def _left(uid, *, user, chat_id=CHAT):
    return make_event(kind="member_left", update_id=uid, chat_id=chat_id, left_user_id=user, user_id=None, text=None)


def _my_member(uid, *, removed, chat_id=CHAT):
    return make_event(
        kind="my_chat_member", update_id=uid, chat_id=chat_id, user_id=SUB, bot_removed=removed,
        left_user_id=BOT_ID if removed else None, text=None,
    )


def _member(uid, *, left_user=None, chat_id=CHAT):
    return make_event(kind="chat_member", update_id=uid, chat_id=chat_id, user_id=SUB, left_user_id=left_user, text=None)


def _migrate(uid, *, chat_id, to=None, frm=None, title=None):
    return make_event(
        kind="message", update_id=uid, chat_id=chat_id, user_id=None, text=None,
        migrate_to_chat_id=to, migrate_from_chat_id=frm, chat_title=title,
    )


async def _q(db, sql, *params):
    async with db.reader() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchall()


# --- bot removed ---


async def test_bot_removed_unregisters_that_chat_only_without_farewell_or_reply(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _my_member(1, removed=True))
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert not await services.is_subscribed(c, CHAT, SUB)
        assert await services.get_chat(c, OTHER) is not None
        assert await services.is_subscribed(c, OTHER, SUB)
        assert await services.get_role(c, ADMIN) is not None
    assert await _q(db, "SELECT COUNT(*) FROM outbox") == [(0,)]
    assert transport.calls == []


async def test_bot_removed_by_left_chat_member_service_message(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _left(1, user=BOT_ID))
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
    assert await _q(db, "SELECT COUNT(*) FROM outbox") == [(0,)]


async def test_readding_the_bot_restores_nothing(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _my_member(1, removed=True))
    await handle_event(ctx, _my_member(2, removed=False))
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert not await services.is_subscribed(c, CHAT, SUB)
    # silence until a new registration, even for subscribers
    await handle_event(ctx, group_event("/upb all", update_id=3, user_id=SUB))
    await handle_event(ctx, group_event("/upb notify on", update_id=4, user_id=SUB))
    assert transport.calls == []
    await handle_event(ctx, group_event("/upb chat register", update_id=5, user_id=ADMIN))
    assert len(transport.calls) == 1


# --- member left ---


async def test_member_left_drops_only_that_subscription_in_that_chat(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _left(1, user=SUB))
    async with db.reader() as c:
        assert not await services.is_subscribed(c, CHAT, SUB)
        assert await services.is_subscribed(c, CHAT, SUB2)
        assert await services.is_subscribed(c, OTHER, SUB)
        assert await services.get_chat(c, CHAT) is not None
    assert transport.calls == []


async def test_chat_member_update_with_leave_unsubscribes_and_restriction_does_not(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _member(1, left_user=None))  # restricted, still a member
    async with db.reader() as c:
        assert await services.is_subscribed(c, CHAT, SUB)
    await handle_event(ctx, _member(2, left_user=SUB))
    async with db.reader() as c:
        assert not await services.is_subscribed(c, CHAT, SUB)


async def test_leaving_admin_keeps_role_and_chat_ownership(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _left(1, user=ADMIN))
    async with db.reader() as c:
        assert (await services.get_chat(c, CHAT)).registered_by == ADMIN


async def test_duplicate_membership_update_is_a_no_op(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _left(1, user=SUB))
    await subscribe(db, services, CHAT, SUB)
    await handle_event(ctx, _left(1, user=SUB))
    async with db.reader() as c:
        assert await services.is_subscribed(c, CHAT, SUB)


async def test_membership_events_for_an_aliased_old_id_are_ignored(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.migrate_chat(c, CHAT, -1000)
    await handle_event(ctx, _my_member(1, removed=True))
    await handle_event(ctx, _left(2, user=SUB))
    await handle_event(ctx, _my_member(3, removed=True, chat_id=CHAT))
    async with db.reader() as c:
        assert await services.get_chat(c, -1000) is not None
        assert await services.is_subscribed(c, -1000, SUB)
    assert await _q(db, "SELECT COUNT(*) FROM processed_updates WHERE outcome = 'ignored'") == [(3,)]


# --- cascades through the command handlers ---


async def test_admin_remove_sends_a_farewell_per_chat_in_each_chat_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, group_event("/upb lang ru", update_id=1, user_id=ADMIN, chat_id=OTHER))
    transport.calls.clear()
    await handle_event(ctx, private_event("/admin remove 1", update_id=2, user_id=ROOT))
    transport.calls.clear()
    await ctx.delivery.run_outbox_once()
    got = sorted((c["chat_id"], c["text"]) for c in transport.calls)
    assert got == [(CHAT, "Chat unregistered. Bye!"), (OTHER, "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0447\u0430\u0442\u0430 \u0441\u043d\u044f\u0442\u0430. \u0414\u043e \u0432\u0441\u0442\u0440\u0435\u0447\u0438!")]
    assert await _q(db, "SELECT COUNT(*) FROM chats") == [(0,)]
    assert await _q(db, "SELECT COUNT(*) FROM subscriptions") == [(0,)]


async def test_set_root_cascade_keeps_the_new_roots_chats(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, 502, ROOT)
    await make_admin(db, services, 11)
    async with db.transaction() as c:
        result = await services.set_root(c, 11)
    assert result.dropped_chat_ids == [502]
    await ctx.delivery.run_outbox_once()
    assert [c["chat_id"] for c in transport.calls] == [502]


# --- migration ---


async def test_migration_moves_registration_language_and_subscriptions(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.set_chat_lang(c, CHAT, "ru")
    await handle_event(ctx, _migrate(1, chat_id=CHAT, to=-1000))
    await handle_event(ctx, _migrate(2, chat_id=-1000, frm=CHAT, title="New"))  # second side: no-op
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        new = await services.get_chat(c, -1000)
        assert (new.registered_by, new.lang) == (ADMIN, "ru")
        assert await services.is_subscribed(c, -1000, SUB) and await services.is_subscribed(c, -1000, SUB2)
        assert await services.resolve_chat_id(c, CHAT) == -1000
    assert transport.calls == []
    await handle_event(ctx, group_event("/upb list", update_id=3, user_id=SUB, chat_id=-1000))
    assert len(transport.calls) == 1


async def test_migration_from_side_first_uses_the_new_title(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _migrate(1, chat_id=-1000, frm=CHAT, title="Renamed"))
    async with db.reader() as c:
        assert (await services.get_chat(c, -1000)).title == "Renamed"


async def test_migration_into_a_registered_destination_keeps_the_destination(db, caplog):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await make_admin(db, services, 2)
    await register_chat(db, services, -1000, 2, "Dest")
    with caplog.at_level(logging.WARNING):
        await handle_event(ctx, _migrate(1, chat_id=CHAT, to=-1000))
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert (await services.get_chat(c, -1000)).registered_by == 2
        assert not await services.is_subscribed(c, -1000, SUB)  # the old subscriptions are not merged
    assert "destination already registered" in caplog.text
    assert transport.calls == []  # no farewell to a chat that moved
    assert await _q(db, "SELECT COUNT(*) FROM outbox") == [(0,)]


async def test_migration_of_an_unregistered_chat_only_records_the_alias(db):
    ctx, services, transport, _c = mk_ctx(db)
    await handle_event(ctx, _migrate(1, chat_id=777, to=-7770))
    async with db.reader() as c:
        assert await services.resolve_chat_id(c, 777) == -7770
        assert await services.get_chat(c, -7770) is None


async def test_contradictory_migration_changes_nothing_and_warns(db, caplog):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _migrate(1, chat_id=CHAT, to=-1000))
    with caplog.at_level(logging.WARNING):
        await handle_event(ctx, _migrate(2, chat_id=CHAT, to=-2000))
    async with db.reader() as c:
        assert await services.get_chat(c, -1000) is not None
        assert await services.get_chat(c, -2000) is None
        assert await services.resolve_chat_id(c, CHAT) == -1000
    assert "contradictory" in caplog.text


async def test_migration_retargets_pending_farewells(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.unregister_chat(c, OTHER)
    await handle_event(ctx, _migrate(1, chat_id=OTHER, to=-1001))
    await ctx.delivery.run_outbox_once()
    assert [c["chat_id"] for c in transport.calls] == [-1001]


async def test_migration_events_do_not_need_a_user_and_are_deduplicated(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    ev = _migrate(1, chat_id=CHAT, to=-1000)
    await handle_event(ctx, ev)
    await handle_event(ctx, ev)
    assert await _q(db, "SELECT COUNT(*) FROM processed_updates") == [(1,)]
