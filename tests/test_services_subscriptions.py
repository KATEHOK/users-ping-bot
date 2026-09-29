from app.db import Database
from app.services import Services


async def _setup_chat(services, db, chat_id=-100, registrar=1):
    async with db.transaction() as c:
        await services.touch_user(c, registrar)
        await services.register_chat(c, chat_id, "Chat", registrar)


async def test_subscribe_creates_and_is_idempotent(db: Database):
    services = Services()
    await _setup_chat(services, db)

    async with db.transaction() as c:
        await services.touch_user(c, 2)
        first = await services.subscribe(c, -100, 2)
    assert first.created is True

    async with db.transaction() as c:
        second = await services.subscribe(c, -100, 2)
    assert second.created is False


async def test_unsubscribe_true_only_when_row_removed(db: Database):
    services = Services()
    await _setup_chat(services, db)
    async with db.transaction() as c:
        await services.touch_user(c, 2)
        await services.subscribe(c, -100, 2)

    async with db.transaction() as c:
        removed = await services.unsubscribe(c, -100, 2)
    assert removed is True

    async with db.transaction() as c:
        removed_again = await services.unsubscribe(c, -100, 2)
    assert removed_again is False


async def test_off_on_cycle_resubscribes(db: Database):
    services = Services()
    await _setup_chat(services, db)
    async with db.transaction() as c:
        await services.touch_user(c, 2)
        await services.subscribe(c, -100, 2)
        await services.unsubscribe(c, -100, 2)

    async with db.transaction() as c:
        second = await services.subscribe(c, -100, 2)

    assert second.created is True
    async with db.reader() as c:
        assert await services.is_subscribed(c, -100, 2)


async def test_subscription_not_shared_across_chats(db: Database):
    services = Services()
    await _setup_chat(services, db, chat_id=-100)
    await _setup_chat(services, db, chat_id=-200)

    async with db.transaction() as c:
        await services.touch_user(c, 2)
        await services.subscribe(c, -100, 2)

    async with db.reader() as c:
        assert not await services.is_subscribed(c, -200, 2)


async def test_list_subscribers_ordered_by_user_id_with_names(db: Database):
    services = Services()
    await _setup_chat(services, db)

    async with db.transaction() as c:
        await services.touch_user(c, 30, username="carol", display_name="Carol")
        await services.touch_user(c, 10, username="alice", display_name="Alice")
        await services.touch_user(c, 20, username="bob", display_name="Bob")
        await services.subscribe(c, -100, 30)
        await services.subscribe(c, -100, 10)
        await services.subscribe(c, -100, 20)

    async with db.reader() as c:
        subs = await services.list_subscribers(c, -100)

    assert [s.user_id for s in subs] == [10, 20, 30]
    assert [s.display_name for s in subs] == ["Alice", "Bob", "Carol"]


async def test_is_subscribed_false_when_absent(db: Database):
    services = Services()
    await _setup_chat(services, db)
    async with db.reader() as c:
        assert not await services.is_subscribed(c, -100, 999)


async def test_list_subscribers_excludes_requested_user(db: Database):
    services = Services()
    await _setup_chat(services, db)
    async with db.transaction() as c:
        for uid in (10, 20, 30):
            await services.touch_user(c, uid, display_name=f"U{uid}")
            await services.subscribe(c, -100, uid)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, -100, exclude_user_id=20)
        only = await services.list_subscribers(c, -100, exclude_user_id=999)
    assert [s.user_id for s in subs] == [10, 30]
    assert [s.user_id for s in only] == [10, 20, 30]
    assert subs[0].display_name == "U10"


async def test_granting_role_does_not_create_a_subscription(db: Database):
    services = Services()
    await _setup_chat(services, db)

    async with db.transaction() as c:
        await services.touch_user(c, 2)
        await services.grant_admin(c, 2)

    async with db.reader() as c:
        assert await services.list_subscribers(c, -100) == []
