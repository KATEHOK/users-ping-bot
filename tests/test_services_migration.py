import pytest

from app.db import Database
from app.models import Role
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


async def test_migrate_chat_moves_registration_generation_registrar_and_subscription_ids(
    db: Database,
):
    services = Services()
    reg = await _register(services, db, 1, -100, "Group")
    async with db.transaction() as c:
        await services.touch_user(c, 2)
        sub = await services.subscribe(c, -100, 2)

    async with db.transaction() as c:
        result = await services.migrate_chat(c, -100, -200)

    assert result.applied is True
    assert result.conflict_id is None

    async with db.reader() as c:
        old_row = await services.get_chat(c, -100)
        new_row = await services.get_chat(c, -200)
        moved_sub_id = await services.subscription_id_of(c, -200, 2)
        gone_sub_id = await services.subscription_id_of(c, -100, 2)
        canonical = await services.resolve_chat_id(c, -100)

    assert old_row is None
    assert new_row is not None
    assert new_row.registered_by == 1
    assert new_row.registration_generation == reg.generation
    assert moved_sub_id == sub.subscription_id
    assert gone_sub_id is None
    assert canonical == -200


async def test_migrate_chat_with_no_active_registration_retargets_pending_farewell(
    db: Database,
):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.unregister_chat(c, -100)  # no chats row left, farewell pending

    async with db.transaction() as c:
        result = await services.migrate_chat(c, -100, -200)
    assert result.applied is True

    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT target_id, status FROM outbox WHERE event_type = 'chat_farewell'"
        )
        rows = await cursor.fetchall()
    assert rows == [(-200, "pending")]

    # a later registration of the new chat_id cancels the re-targeted farewell
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, -200, "New group", 1)
    async with db.reader() as c:
        cursor = await c.execute("SELECT status FROM outbox WHERE event_type = 'chat_farewell'")
        (status,) = await cursor.fetchone()
    assert status == "cancelled"


async def test_repeated_migration_is_noop(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        first = await services.migrate_chat(c, -100, -200)
    assert first.applied is True

    async with db.transaction() as c:
        second = await services.migrate_chat(c, -100, -200)
    assert second.applied is False
    assert second.conflict_id is None

    async with db.reader() as c:
        assert await services.list_conflicts(c) == []


async def test_paired_migration_updates_change_nothing_after_first(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.touch_user(c, 2)
        sub = await services.subscribe(c, -100, 2)

    async with db.transaction() as c:
        await services.migrate_chat(c, -100, -200)
    async with db.reader() as c:
        row_after_first = await services.get_chat(c, -200)

    async with db.transaction() as c:
        await services.migrate_chat(c, -100, -200)  # duplicate update, different update_id
    async with db.reader() as c:
        row_after_second = await services.get_chat(c, -200)
        sub_id = await services.subscription_id_of(c, -200, 2)

    assert row_after_first == row_after_second
    assert sub_id == sub.subscription_id


async def test_migration_contradictory_alias_opens_conflict_and_blocks_both_sides(
    db: Database,
):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.migrate_chat(c, -100, -200)

    async with db.transaction() as c:
        # a second, contradictory migration for the same old_chat_id
        result = await services.migrate_chat(c, -100, -300)
    assert result.applied is False
    assert result.conflict_id is not None

    async with db.reader() as c:
        blocked_old = await services.is_blocked(c, -100)
        blocked_new = await services.is_blocked(c, -200)
        blocked_other = await services.is_blocked(c, -300)
    assert blocked_old is True
    assert blocked_new is True
    assert blocked_other is True


async def test_migration_destination_conflict_cancels_scope_events_and_persists(
    db: Database, tmp_path
):
    path = str(tmp_path / "persist.sqlite3")
    from app.db import apply_migrations

    database = Database(path)
    await database.connect()
    await apply_migrations(database)
    services = Services()
    try:
        await _register(services, database, 1, -100, "Old")
        await _register(services, database, 2, -200, "Independent")  # unrelated registration

        async with database.transaction() as c:
            result = await services.migrate_chat(c, -100, -200)
        assert result.applied is False
        assert result.conflict_id is not None

        async with database.reader() as c:
            events_cancelled_count = await services.cancel_chat_events(c, -100)  # already 0 left
        assert events_cancelled_count == 0  # nothing pending anymore (both cancelled already)
    finally:
        await database.close()

    # reopen a fresh Database instance on the same file: block must survive
    database2 = Database(path)
    await database2.connect()
    try:
        async with database2.reader() as c:
            blocked = await services.is_blocked(c, -100)
            blocked_dest = await services.is_blocked(c, -200)
        assert blocked is True
        assert blocked_dest is True
    finally:
        await database2.close()


async def test_conflict_scope_lists_connected_group_and_overlapping_conflicts(db: Database):
    services = Services()
    async with db.transaction() as c:
        first_id = await services.open_conflict(c, [-100, -200], "manual")
    async with db.transaction() as c:
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (-200, -300, 't')"
        )
        second_id = await services.open_conflict(c, [-300, -400], "manual")

    async with db.reader() as c:
        scope = await services.conflict_scope(c, first_id)

    assert scope.chat_ids == sorted([-100, -200, -300, -400])
    assert sorted(scope.conflict_ids) == sorted([first_id, second_id])
    assert (-200, -300) in scope.alias_pairs


async def test_reset_conflict_removes_only_its_scope(db: Database):
    services = Services()
    await _register(services, db, 1, -100, "A")
    await _register(services, db, 1, -300, "Untouched")
    async with db.transaction() as c:
        await services.touch_user(c, 5)
        await services.grant_admin(c, 5)
        await services.subscribe(c, -100, 5)
        await services.subscribe(c, -300, 5)
        conflict_id = await services.open_conflict(c, [-100, -200], "manual")

    async with db.transaction() as c:
        result = await services.reset_conflict(c, conflict_id)

    assert sorted(result.chat_ids) == [-200, -100]
    assert result.chats_removed == 1  # only -100 had a chats row; -200 never existed
    assert result.subscriptions_removed == 1

    async with db.reader() as c:
        assert await services.get_chat(c, -100) is None
        untouched = await services.get_chat(c, -300)
        assert untouched is not None
        assert await services.subscription_id_of(c, -300, 5) is not None  # unrelated sub kept
        assert await services.get_role(c, 5) is Role.ADMIN  # global role untouched
        assert await services.is_blocked(c, -100) is False
        conflicts = await services.list_conflicts(c, status="resolved")
    assert len(conflicts) == 1
    assert conflicts[0].conflict_id == conflict_id


async def test_reset_conflict_does_not_activate_a_chat(db: Database):
    services = Services()
    async with db.transaction() as c:
        conflict_id = await services.open_conflict(c, [-100, -200], "manual")
    async with db.transaction() as c:
        await services.reset_conflict(c, conflict_id)
    async with db.reader() as c:
        assert await services.get_chat(c, -100) is None
        assert await services.get_chat(c, -200) is None


async def test_reset_conflict_keeps_processed_updates_and_repeat_update_is_safe(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        assert await services.claim_update(c, 7, 42) is True
        conflict_id = await services.open_conflict(c, [-100], "manual")

    async with db.transaction() as c:
        await services.reset_conflict(c, conflict_id)

    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT COUNT(*) FROM processed_updates WHERE bot_id = 7 AND update_id = 42"
        )
        (count,) = await cursor.fetchone()
    assert count == 1  # processed_updates untouched by reset

    # repeat of the old update is a safe no-op
    async with db.transaction() as c:
        claimed_again = await services.claim_update(c, 7, 42)
    assert claimed_again is False


async def test_failure_inside_reset_conflict_leaves_conflict_open_and_db_unchanged(
    db: Database,
):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        conflict_id = await services.open_conflict(c, [-100], "manual")

    with pytest.raises(RuntimeError):
        async with db.transaction() as c:
            await services.reset_conflict(c, conflict_id)
            raise RuntimeError("boom mid-reset")

    async with db.reader() as c:
        conflicts = await services.list_conflicts(c, status="open")
        chat = await services.get_chat(c, -100)
    assert len(conflicts) == 1
    assert conflicts[0].conflict_id == conflict_id
    assert chat is not None  # nothing was actually removed


async def test_failure_inside_migrate_chat_leaves_no_alias_and_no_conflict(db: Database):
    services = Services()
    await _register(services, db, 1, -100)

    with pytest.raises(RuntimeError):
        async with db.transaction() as c:
            await services.migrate_chat(c, -100, -200)
            raise RuntimeError("boom mid-migration")

    async with db.reader() as c:
        canonical = await services.resolve_chat_id(c, -100)
        old_row = await services.get_chat(c, -100)
        conflicts = await services.list_conflicts(c)
    assert canonical == -100  # no alias committed
    assert old_row is not None  # old registration untouched
    assert conflicts == []


async def test_is_blocked_true_for_any_id_in_the_alias_group(db: Database):
    services = Services()
    async with db.transaction() as c:
        conflict_id = await services.open_conflict(c, [-100], "manual")
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (-50, -100, 't')"
        )
    async with db.reader() as c:
        assert await services.is_blocked(c, -100) is True
        assert await services.is_blocked(c, -50) is True  # alias into the blocked id
        assert await services.is_blocked(c, -999) is False
    assert conflict_id is not None
