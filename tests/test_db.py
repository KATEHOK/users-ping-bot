import sqlite3

import pytest

from app.db import Database, apply_migrations, open_database


async def test_apply_migrations_idempotent(db: Database):
    # db fixture already ran migrations once
    applied_again = await apply_migrations(db)
    assert applied_again == []


async def test_migrations_seed_counters(db: Database):
    async with db.reader() as conn:
        cursor = await conn.execute("SELECT name, value FROM counters ORDER BY name")
        rows = await cursor.fetchall()
    assert rows == [("generation", 0), ("subscription_id", 0)]


async def test_schema_migrations_recorded(db: Database):
    async with db.reader() as conn:
        cursor = await conn.execute("SELECT version FROM schema_migrations")
        rows = await cursor.fetchall()
    assert rows == [("001_initial",)]


async def test_expected_tables_exist(db: Database):
    expected = {
        "users",
        "roles",
        "chats",
        "subscriptions",
        "outbox",
        "processed_updates",
        "polling_state",
        "chat_aliases",
        "migration_conflicts",
        "counters",
        "schema_migrations",
    }
    async with db.reader() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {row[0] for row in await cursor.fetchall()}
    assert expected <= names


async def test_foreign_keys_cascade_chat_delete(db: Database):
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO users(user_id, updated_at) VALUES (1, 'now')"
        )
        await conn.execute(
            "INSERT INTO chats(chat_id, title, registered_by, registered_at, "
            "registration_generation) VALUES (-100, 't', 1, 'now', 1)"
        )
        await conn.execute(
            "INSERT INTO subscriptions(chat_id, user_id, subscription_id, created_at) "
            "VALUES (-100, 1, 1, 'now')"
        )

    async with db.transaction() as conn:
        await conn.execute("DELETE FROM chats WHERE chat_id = -100")

    async with db.reader() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE chat_id = -100"
        )
        (count,) = await cursor.fetchone()
    assert count == 0


async def test_single_root_enforced(db: Database):
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO users(user_id, updated_at) VALUES (1, 'now')")
        await conn.execute("INSERT INTO users(user_id, updated_at) VALUES (2, 'now')")
        await conn.execute(
            "INSERT INTO roles(user_id, role, granted_at) VALUES (1, 'root', 'now')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO roles(user_id, role, granted_at) VALUES (2, 'root', 'now')"
            )


async def test_transaction_rolls_back_on_exception(db: Database):
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO users(user_id, updated_at) VALUES (99, 'now')")
            raise RuntimeError("boom")

    async with db.reader() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM users WHERE user_id = 99")
        (count,) = await cursor.fetchone()
    assert count == 0


async def test_open_database_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "upb.sqlite3"
    assert not path.parent.exists()
    async with open_database(str(path)) as database:
        async with database.reader() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM schema_migrations")
            (count,) = await cursor.fetchone()
        assert count == 1
    assert path.exists()


async def test_pragmas_applied(db: Database):
    async with db.reader() as conn:
        cursor = await conn.execute("PRAGMA foreign_keys")
        (fk,) = await cursor.fetchone()
        cursor = await conn.execute("PRAGMA journal_mode")
        (journal_mode,) = await cursor.fetchone()
        cursor = await conn.execute("PRAGMA synchronous")
        (synchronous,) = await cursor.fetchone()
    assert fk == 1
    assert journal_mode.lower() == "wal"
    assert synchronous == 2  # FULL
