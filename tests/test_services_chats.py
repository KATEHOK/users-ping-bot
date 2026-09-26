from app.db import Database
from app.models import Role
from app.services import Services


async def _touch_and_register(services, db, user_id, chat_id, title="Chat"):
    async with db.transaction() as c:
        await services.touch_user(c, user_id)
        result = await services.register_chat(c, chat_id, title, user_id)
    return result


async def test_register_chat_creates_new(db: Database):
    services = Services()
    result = await _touch_and_register(services, db, 1, -100)
    assert result.created is True
    assert result.generation == 1


async def test_two_sequential_registrations_keep_first_registrar_and_generation(db: Database):
    services = Services()
    first = await _touch_and_register(services, db, 1, -100)

    async with db.transaction() as c:
        await services.touch_user(c, 2)
        # second registrar attempts to register the same already-active chat
        second = await services.register_chat(c, -100, "Renamed", 2)

    assert second.created is False
    assert second.generation == first.generation

    async with db.reader() as c:
        row = await services.get_chat(c, -100)
    assert row is not None
    assert row.registered_by == 1  # registrar unchanged, not the second caller
    assert row.title == "Chat"  # title unchanged too


async def test_register_chat_idempotent_preserves_subscriptions(db: Database):
    services = Services()
    await _touch_and_register(services, db, 1, -100)

    async with db.transaction() as c:
        await services.touch_user(c, 2)
        sub = await services.subscribe(c, -100, 2)

    async with db.transaction() as c:
        await services.register_chat(c, -100, "Chat", 1)

    async with db.reader() as c:
        still = await services.subscription_id_of(c, -100, 2)
    assert still == sub.subscription_id


async def test_register_chat_allocates_new_generation_after_recreate(db: Database):
    services = Services()
    first = await _touch_and_register(services, db, 1, -100)

    async with db.transaction() as c:
        await services.unregister_chat(c, -100)

    async with db.transaction() as c:
        second = await services.register_chat(c, -100, "Chat", 1)

    assert second.created is True
    assert second.generation > first.generation


async def test_unregister_chat_cascades_subscriptions(db: Database):
    services = Services()
    await _touch_and_register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.touch_user(c, 2)
        await services.touch_user(c, 3)
        await services.subscribe(c, -100, 2)
        await services.subscribe(c, -100, 3)

    async with db.transaction() as c:
        result = await services.unregister_chat(c, -100)

    assert result.chat_id == -100
    assert result.subscriptions_removed == 2

    async with db.reader() as c:
        assert await services.get_chat(c, -100) is None
        subs = await services.list_subscribers(c, -100)
    assert subs == []


async def test_unregister_nonexistent_chat_is_noop(db: Database):
    services = Services()
    async with db.transaction() as c:
        result = await services.unregister_chat(c, -999)
    assert result.subscriptions_removed == 0
    assert result.generation == 0


async def test_list_chats_ordered_and_blocked_false(db: Database):
    services = Services()
    await _touch_and_register(services, db, 1, -200, "B")
    await _touch_and_register(services, db, 1, -100, "A")

    async with db.reader() as c:
        rows = await services.list_chats(c)

    assert [r.chat_id for r in rows] == [-200, -100]
    assert all(r.blocked is False for r in rows)


async def test_admin_b_can_unregister_chat_registered_by_admin_a_without_touching_a(
    db: Database,
):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.touch_user(c, 2)
        await services.grant_admin(c, 1)  # A
        await services.grant_admin(c, 2)  # B
        await services.register_chat(c, -100, "A's chat 1", 1)
        await services.register_chat(c, -200, "A's chat 2", 1)

    # B (a different admin) unregisters one of A's chats; services has no actor
    # parameter here, mirroring that the access check happens in the handler layer
    async with db.transaction() as c:
        await services.unregister_chat(c, -100)

    async with db.reader() as c:
        role_a = await services.get_role(c, 1)
        remaining = await services.get_chat(c, -200)
        gone = await services.get_chat(c, -100)

    assert role_a is Role.ADMIN  # A keeps the admin role
    assert remaining is not None  # A's other chat survives
    assert remaining.registered_by == 1
    assert gone is None
