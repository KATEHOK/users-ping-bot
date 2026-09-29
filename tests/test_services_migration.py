import logging

import pytest

from app.db import Database
from app.services import Services


async def _register(services, db, user_id, chat_id, title="Chat"):
    async with db.transaction() as c:
        await services.touch_user(c, user_id)
        return await services.register_chat(c, chat_id, title, user_id)


async def test_resolve_chat_id_follows_chain_and_is_stable_for_unknown(db: Database):
    services = Services()
    async with db.transaction() as c:
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (-1, -2, 't')"
        )
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (-2, -3, 't')"
        )
    async with db.reader() as c:
        assert await services.resolve_chat_id(c, -1) == -3
        assert await services.resolve_chat_id(c, -2) == -3
        assert await services.resolve_chat_id(c, -3) == -3
        assert await services.resolve_chat_id(c, -999) == -999  # unknown: returned as-is


async def test_resolve_chat_id_tolerates_cycle(db: Database):
    services = Services()
    async with db.transaction() as c:
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (-1, -2, 't')"
        )
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (-2, -1, 't')"
        )
    async with db.reader() as c:
        # must terminate rather than loop forever
        result = await services.resolve_chat_id(c, -1)
    assert result in (-1, -2)


async def _sub(services, db, chat_id, user_id):
    async with db.transaction() as c:
        await services.touch_user(c, user_id)
        await services.subscribe(c, chat_id, user_id)


async def test_migrate_moves_registration_generation_registrar_lang_and_subscriptions(
    db: Database,
):
    services = Services()
    reg = await _register(services, db, 1, -100, "Group")
    async with db.transaction() as c:
        await services.set_chat_lang(c, -100, "ru")
    await _sub(services, db, -100, 2)

    async with db.transaction() as c:
        result = await services.migrate_chat(c, -100, -200)
    assert result.action == "moved"

    async with db.reader() as c:
        old_row = await services.get_chat(c, -100)
        new_row = await services.get_chat(c, -200)
        moved = await services.is_subscribed(c, -200, 2)
        gone = await services.is_subscribed(c, -100, 2)
        canonical = await services.resolve_chat_id(c, -100)

    assert old_row is None
    assert new_row is not None
    assert new_row.registered_by == 1
    assert new_row.registration_generation == reg.generation
    assert new_row.lang == "ru"
    assert moved and not gone
    assert canonical == -200


async def test_migrate_keeps_destination_and_drops_old_registration_with_warning(
    db: Database, caplog
):
    services = Services()
    await _register(services, db, 1, -100, "Old")
    await _sub(services, db, -100, 5)
    await _register(services, db, 2, -200, "New")
    await _sub(services, db, -200, 6)

    with caplog.at_level(logging.WARNING, logger="app.services"):
        async with db.transaction() as c:
            result = await services.migrate_chat(c, -100, -200)
    assert result.action == "kept_destination"
    assert any(r.levelno == logging.WARNING for r in caplog.records)

    async with db.reader() as c:
        assert await services.get_chat(c, -100) is None
        new_row = await services.get_chat(c, -200)
        assert new_row is not None and new_row.registered_by == 2
        assert await services.is_subscribed(c, -200, 6)
        assert not await services.is_subscribed(c, -200, 5)  # old subscribers are not merged
        assert await services.resolve_chat_id(c, -100) == -200
        cursor = await c.execute("SELECT COUNT(*) FROM outbox")
        assert (await cursor.fetchone())[0] == 0  # no farewell to a chat that moved


async def test_migrate_alias_only_when_old_chat_unregistered(db: Database):
    services = Services()
    async with db.transaction() as c:
        result = await services.migrate_chat(c, -100, -200)
    assert result.action == "alias_only"
    async with db.reader() as c:
        assert await services.get_chat(c, -200) is None
        assert await services.resolve_chat_id(c, -100) == -200


async def test_migrate_retargets_pending_farewell_and_reregistration_cancels_it(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.unregister_chat(c, -100)  # no chats row left, farewell pending

    async with db.transaction() as c:
        result = await services.migrate_chat(c, -100, -200)
    assert result.action == "alias_only"

    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT target_id, status FROM outbox WHERE event_type = 'chat_farewell'"
        )
        rows = await cursor.fetchall()
    assert rows == [(-200, "pending")]

    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, -200, "New group", 1)
    async with db.reader() as c:
        cursor = await c.execute("SELECT status FROM outbox WHERE event_type = 'chat_farewell'")
        (status,) = await cursor.fetchone()
    assert status == "cancelled"


async def test_migrate_retargets_pending_events_when_moving_registration(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.queue_event(
            c, event_key="x", event_type="chat_farewell", target_kind="chat",
            target_id=-100, payload={},
        )
        await services.migrate_chat(c, -100, -200)
    async with db.reader() as c:
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert [e.target_id for e in events] == [-200]


async def test_repeated_migration_is_noop(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        first = await services.migrate_chat(c, -100, -200)
    assert first.action == "moved"
    async with db.reader() as c:
        row_after_first = await services.get_chat(c, -200)

    async with db.transaction() as c:
        second = await services.migrate_chat(c, -100, -200)
    assert second.action == "noop"
    async with db.reader() as c:
        assert await services.get_chat(c, -200) == row_after_first


async def test_contradictory_alias_warns_and_changes_nothing(db: Database, caplog):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.migrate_chat(c, -100, -200)
    await _register(services, db, 2, -300)

    with caplog.at_level(logging.WARNING, logger="app.services"):
        async with db.transaction() as c:
            result = await services.migrate_chat(c, -100, -300)
    assert result.action == "contradictory"
    assert any(r.levelno == logging.WARNING for r in caplog.records)

    async with db.reader() as c:
        assert await services.resolve_chat_id(c, -100) == -200
        assert await services.get_chat(c, -200) is not None
        assert await services.get_chat(c, -300) is not None


async def test_failure_inside_migrate_chat_leaves_no_alias(db: Database):
    services = Services()
    await _register(services, db, 1, -100)

    with pytest.raises(RuntimeError):
        async with db.transaction() as c:
            await services.migrate_chat(c, -100, -200)
            raise RuntimeError("boom mid-migration")

    async with db.reader() as c:
        assert await services.resolve_chat_id(c, -100) == -100
        assert await services.get_chat(c, -100) is not None


async def _alias_count(db):
    async with db.reader() as c:
        cur = await c.execute("SELECT COUNT(*) FROM chat_aliases")
        return (await cur.fetchone())[0]


async def test_migrating_a_chat_to_itself_is_a_noop(db: Database):
    services = Services()
    await _register(services, db, 1, -100, "Group")
    async with db.transaction() as c:
        result = await services.migrate_chat(c, -100, -100)
    assert result.action == "noop"
    assert await _alias_count(db) == 0
    async with db.reader() as c:
        assert await services.get_chat(c, -100) is not None
