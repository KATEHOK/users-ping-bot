"""Recorded personal (member-scope) menus: written after a successful call, read on revoke."""

import logging

from app import rendering
from app.db import MIGRATIONS_DIR, Database, _split_statements, apply_migrations
from app.delivery import ChatMigrated, PermanentSend, RateLimited
from app.handlers import handle_event

from conftest import group_event, make_admin, make_event, make_root, mk_ctx, private_event, register_chat

ROOT = 10
ADMIN = 1
CHAT_ID = 500
FREE = -700  # a group the bot was added to and nobody registered


async def _rows(db):
    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT chat_id, user_id, kind FROM member_menus ORDER BY chat_id, user_id"
        )
        return await cursor.fetchall()


def _joined(chat_id, user_id):
    return make_event(
        kind="my_chat_member", update_id=1, chat_id=chat_id, user_id=user_id,
        chat_type="group", bot_added=True, text=None,
    )


async def test_migration_002_applies_on_a_v11_database(tmp_path):
    path = str(tmp_path / "v11.sqlite3")
    database = Database(path)
    await database.connect()
    async with database.transaction() as c:
        for stmt in _split_statements((MIGRATIONS_DIR / "001_initial.sql").read_text()):
            await c.execute(stmt)
        await c.execute("INSERT INTO schema_migrations VALUES ('001_initial', 'x')")
        await c.execute("INSERT INTO users(user_id, updated_at) VALUES (5, 'x')")
    assert await apply_migrations(database) == ["002_member_menus"]
    async with database.reader() as c:
        cursor = await c.execute("SELECT COUNT(*) FROM users")
        assert (await cursor.fetchone())[0] == 1  # existing data is untouched
        cursor = await c.execute("SELECT COUNT(*) FROM member_menus")
        assert (await cursor.fetchone())[0] == 0
    assert await apply_migrations(database) == []
    await database.close()


async def test_rows_are_recorded_after_set_and_dropped_after_delete(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    await make_admin(db, services, ADMIN)
    await handle_event(ctx, _joined(FREE, ADMIN))
    assert sorted(await _rows(db)) == [(FREE, ADMIN, "register"), (FREE, ROOT, "register")]
    await handle_event(ctx, group_event("/register", update_id=2, user_id=ADMIN, chat_id=FREE))
    assert sorted(await _rows(db)) == [(FREE, ADMIN, "owner"), (FREE, ROOT, "owner")]
    await handle_event(ctx, group_event("/unregister", update_id=3, user_id=ADMIN, chat_id=FREE))
    assert {r[2] for r in await _rows(db)} == {"register"}
    async with db.transaction() as c:
        await services.revoke_admin(c, ADMIN)
    await ctx.delivery.sync_chat_menu(FREE, users=[ADMIN, ROOT])
    assert await _rows(db) == [(FREE, ROOT, "register")]


async def test_no_row_is_written_when_the_call_fails(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    transport.fail_menu(PermanentSend())
    await handle_event(ctx, _joined(FREE, ROOT))
    assert await _rows(db) == []
    # a failed delete keeps the row
    async with db.transaction() as c:
        await services.record_member_menu(c, FREE, 77, "register")
    await ctx.delivery.sync_chat_menu(FREE, users=[77])
    assert await _rows(db) == [(FREE, 77, "register")]


async def test_revoke_deletes_in_an_unregistered_recorded_group_only(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    await make_admin(db, services, ADMIN)
    await handle_event(ctx, _joined(FREE, ADMIN))
    await register_chat(db, services, CHAT_ID, ROOT)  # an unrelated active chat
    transport.menu_calls.clear()
    await handle_event(ctx, private_event(f"/admin remove {ADMIN}", update_id=2, user_id=ROOT))
    calls = [(c["op"], c["chat_id"], c["user_id"]) for c in transport.menu_calls]
    assert calls == [("delete", FREE, ADMIN)]
    assert await _rows(db) == [(FREE, ROOT, "register")]


async def test_root_change_deletes_the_old_root_menu_in_the_recorded_group(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await handle_event(ctx, _joined(FREE, ROOT))
    async with db.transaction() as c:  # the CLI: no transport
        await services.touch_user(c, 11)
        await services.set_root(c, 11)
    transport.menu_calls.clear()
    await ctx.delivery.run_outbox_once()
    deletes = [(c["chat_id"], c["user_id"]) for c in transport.menu_calls if c["op"] == "delete"]
    assert (FREE, ROOT) in deletes
    assert all(r[1] != ROOT for r in await _rows(db))


async def test_chat_migration_moves_the_rows(db):
    ctx, services, transport, _c = mk_ctx(db)
    async with db.transaction() as c:
        await services.touch_user(c, ADMIN)
        await services.record_member_menu(c, FREE, ADMIN, "register")
        await services.record_member_menu(c, FREE, ROOT, "register")
        await services.record_member_menu(c, -1234, ROOT, "owner")  # the destination has one
        await services.record_member_menu(c, 999, ADMIN, "register")  # unrelated
        await services.migrate_chat(c, FREE, -1234)
    assert await _rows(db) == [
        (-1234, ADMIN, "register"), (-1234, ROOT, "owner"), (999, ADMIN, "register"),
    ]


async def test_revoke_finds_a_registered_chat_that_migrated(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    await make_admin(db, services, ADMIN)
    await make_admin(db, services, 2)
    await register_chat(db, services, CHAT_ID, 2)
    async with db.transaction() as c:
        await services.record_member_menu(c, CHAT_ID, ADMIN, "register")
    transport.queue_menu_raises([ChatMigrated(-1001)])
    await handle_event(ctx, private_event(f"/admin remove {ADMIN}", update_id=1, user_id=ROOT))
    async with db.reader() as c:
        assert await services.get_chat(c, -1001) is not None  # the migration was recorded
        assert await services.get_chat(c, CHAT_ID) is None
    assert ("delete", -1001, ADMIN) in [
        (c["op"], c["chat_id"], c["user_id"]) for c in transport.menu_calls
    ]


async def test_a_failed_row_write_is_logged_and_does_not_block(db, caplog):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    async with db.transaction() as c:
        await c.execute("DROP TABLE member_menus")
    with caplog.at_level(logging.DEBUG):
        ok = await ctx.delivery.sync_chat_menu(FREE, users=[ROOT])
    assert ok is True  # the menu itself was set
    assert [(c["op"], c["user_id"]) for c in transport.menu_calls] == [("delete", None), ("set", ROOT)]
    assert any(r.getMessage().startswith("menu_record_error exc=OperationalError") for r in caplog.records)


async def test_a_429_over_the_cap_keeps_the_row_and_waits_for_nothing(db):
    ctx, services, transport, _c = mk_ctx(db)
    async with db.transaction() as c:
        await services.record_member_menu(c, FREE, ROOT, "register")
    transport.queue_menu_raises([RateLimited(100)])
    await ctx.delivery.delete_member_menus([FREE], ROOT, single_attempt=True)
    assert len(transport.menu_calls) == 1
    assert await _rows(db) == [(FREE, ROOT, "register")]


async def test_menu_text_kinds_match_the_recorded_kind(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT)
    await ctx.delivery.sync_chat_menu(FREE, users=[ROOT])
    assert transport.menu_calls[-1]["commands"] == rendering.register_menu_commands("en")
    assert await _rows(db) == [(FREE, ROOT, "register")]
