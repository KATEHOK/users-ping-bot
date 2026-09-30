import logging
import sqlite3

import pytest

from app.db import Database, SchemaTooNewError, _split_statements, apply_migrations, open_database


async def test_apply_migrations_idempotent(db: Database):
    # db fixture already ran migrations once
    applied_again = await apply_migrations(db)
    assert applied_again == []


async def test_migrations_seed_counters(db: Database):
    async with db.reader() as conn:
        cursor = await conn.execute("SELECT name, value FROM counters ORDER BY name")
        rows = await cursor.fetchall()
    assert rows == [("generation", 0)]


async def test_schema_migrations_recorded(db: Database):
    async with db.reader() as conn:
        cursor = await conn.execute("SELECT version FROM schema_migrations")
        rows = await cursor.fetchall()
    assert rows == [("001_initial",), ("002_member_menus",), ("003_chat_names",)]


async def test_expected_tables_exist(db: Database):
    expected = {
        "users",
        "roles",
        "chats",
        "subscriptions",
        "outbox",
        "processed_updates",
        "chat_aliases",
        "chat_names",
        "counters",
        "schema_migrations",
    }
    async with db.reader() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {row[0] for row in await cursor.fetchall()}
    assert expected <= names
    assert not {"polling_state", "migration_conflicts"} & names


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
            "INSERT INTO subscriptions(chat_id, user_id, created_at) "
            "VALUES (-100, 1, 'now')"
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
        assert count == 3
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


async def test_lang_columns_default_to_en(db: Database):
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO users(user_id, updated_at) VALUES (1, 'now')")
        await conn.execute(
            "INSERT INTO chats(chat_id, title, registered_by, registered_at, "
            "registration_generation) VALUES (-100, 't', 1, 'now', 1)"
        )
        cursor = await conn.execute("SELECT lang FROM users")
        assert (await cursor.fetchone())[0] == "en"
        cursor = await conn.execute("SELECT lang FROM chats")
        assert (await cursor.fetchone())[0] == "en"


async def test_processed_updates_has_processed_at_index(db: Database):
    async with db.reader() as conn:
        cursor = await conn.execute("PRAGMA index_list(processed_updates)")
        indexes = [row[1] for row in await cursor.fetchall()]
        assert indexes
        cursor = await conn.execute(f"PRAGMA index_info({indexes[0]})")
        cols = [row[2] for row in await cursor.fetchall()]
    assert "processed_at" in cols


async def test_subscriptions_have_no_subscription_id_column(db: Database):
    async with db.reader() as conn:
        cursor = await conn.execute("PRAGMA table_info(subscriptions)")
        cols = {row[1] for row in await cursor.fetchall()}
    assert cols == {"chat_id", "user_id", "created_at"}


def test_split_statements_keeps_semicolon_inside_string_literal():
    sql = (
        "-- leading; comment\n"
        "INSERT INTO t VALUES ('a;b');\n"
        "INSERT INTO t VALUES ('multi\nline; text');\n"
        "-- trailing comment; only\n"
    )
    statements = _split_statements(sql)
    assert len(statements) == 2
    assert "'a;b'" in statements[0]
    assert "'multi\nline; text'" in statements[1]


def test_split_statements_rejects_truncated_statement():
    with pytest.raises(ValueError):
        _split_statements("SELECT 1; SELECT 'oops")


async def test_schema_too_new_is_refused(tmp_path):
    path = str(tmp_path / "future.sqlite3")
    async with open_database(path) as database:
        async with database.transaction() as conn:
            await conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES ('999_future', 'now')"
            )

    with pytest.raises(SchemaTooNewError):
        async with open_database(path):
            pass


async def test_created_new_and_warning_with_absolute_path(tmp_path, caplog, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.db"):
        database = Database("rel.sqlite3")
        await database.connect()
        assert database.created_new is True
        await database.close()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(tmp_path / "rel.sqlite3") in warnings[0]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.db"):
        again = Database("rel.sqlite3")
        await again.connect()
        assert again.created_new is False
        await again.close()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_in_memory_database_is_not_reported_as_new():
    database = Database(":memory:")
    await database.connect()
    assert database.created_new is False
    await database.close()
