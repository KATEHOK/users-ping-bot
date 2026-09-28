import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import cli
from app.db import open_database
from app.services import Services


def _write(db_path: str, fn):
    async def runner():
        async with open_database(db_path) as db:
            async with db.transaction() as c:
                return await fn(c)

    return asyncio.run(runner())


def _dump(db_path: str, table: str, order_by: str):
    async def runner():
        async with open_database(db_path) as db:
            async with db.reader() as c:
                cursor = await c.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
                return await cursor.fetchall()

    return asyncio.run(runner())


def _db_path(tmp_path) -> str:
    return str(tmp_path / "live.sqlite3")


async def _seed_full_state(c) -> None:
    services = Services()
    await services.touch_user(c, 1, display_name="Root")
    await services.set_root(c, 1)
    await services.touch_user(c, 2, display_name="Admin")
    await services.grant_admin(c, 2)
    await services.register_chat(c, -100, "Group", 2)
    await services.touch_user(c, 3, display_name="Alice")
    await services.subscribe(c, -100, 3)
    await services.touch_user(c, 4, display_name="Bob")
    await services.subscribe(c, -100, 4)
    await services.queue_event(
        c,
        event_key="farewell:-200:1",
        event_type="chat_farewell",
        target_kind="chat",
        target_id=-200,
        generation=1,
        payload={},
    )


# --- backup: happy path and argument handling ---


def test_backup_creates_file_and_prints_summary(tmp_path, monkeypatch, capsys):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, lambda c: Services().touch_user(c, 1))

    dest = tmp_path / "out" / "backup.sqlite3"
    assert cli.main(["backup", str(dest)]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"db={Path(src).resolve()}"
    assert f"source={Path(src).resolve()}" in out
    assert f"destination={dest.resolve()}" in out
    assert "bytes=" in out
    assert "pages=" in out
    assert dest.is_file()
    # "bytes=" printed must match the real file size, and be > 0
    byte_line = next(line for line in out.splitlines() if line.startswith("bytes="))
    assert int(byte_line.split("=", 1)[1]) == dest.stat().st_size > 0


def test_backup_creates_missing_parent_directory(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, lambda c: Services().touch_user(c, 1))

    dest = tmp_path / "nested" / "deeper" / "backup.sqlite3"
    assert not dest.parent.exists()
    assert cli.main(["backup", str(dest)]) == 0
    assert dest.is_file()


def test_backup_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, lambda c: Services().touch_user(c, 1))

    dest = tmp_path / "backup.sqlite3"
    dest.write_text("pre-existing content")

    rc = cli.main(["backup", str(dest)])
    assert rc == 2
    assert dest.read_text() == "pre-existing content"  # left untouched

    rc = cli.main(["backup", str(dest), "--force"])
    assert rc == 0
    assert dest.read_bytes() != b"pre-existing content"


def test_backup_missing_source_database_exits_nonzero(tmp_path, monkeypatch):
    src = _db_path(tmp_path)  # never created
    monkeypatch.setenv("UPB_DB_PATH", src)

    dest = tmp_path / "backup.sqlite3"
    assert cli.main(["backup", str(dest)]) != 0
    assert not dest.exists()


def test_backup_refuses_same_file_even_with_force(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)
    before = _dump(src, "roles", "user_id")

    assert cli.main(["backup", src]) == 2
    assert cli.main(["backup", src, "--force"]) == 2
    # same file through a different spelling / a symlink
    alias = tmp_path / "sub" / ".." / "live.sqlite3"
    assert cli.main(["backup", str(alias), "--force"]) == 2
    link = tmp_path / "link.sqlite3"
    link.symlink_to(src)
    assert cli.main(["backup", str(link), "--force"]) == 2

    assert _dump(src, "roles", "user_id") == before
    assert cli.main(["verify", src]) == 0


def test_backup_works_with_special_characters_in_paths(tmp_path, monkeypatch):
    weird = tmp_path / "we?ird #dir%41"
    weird.mkdir()
    src = str(weird / "live?db#1.sqlite3")
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)

    dest = weird / "copy?.sqlite3"
    assert cli.main(["backup", str(dest)]) == 0
    assert _dump(src, "roles", "user_id") == _dump(str(dest), "roles", "user_id")
    assert cli.main(["verify", str(dest)]) == 0


def test_backup_is_single_file_in_delete_journal_mode(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)  # source is WAL

    dest = tmp_path / "out" / "backup.sqlite3"
    assert cli.main(["backup", str(dest)]) == 0

    assert sorted(p.name for p in dest.parent.iterdir()) == ["backup.sqlite3"]
    conn = sqlite3.connect(f"{dest.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert not Path(f"{dest}-wal").exists() and not Path(f"{dest}-shm").exists()


# --- backup: restorability and content fidelity ---


def test_backup_restore_matches_roles_chats_subscriptions_outbox(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)

    dest = str(tmp_path / "backup.sqlite3")
    assert cli.main(["backup", dest]) == 0

    for table, order_by in [
        ("roles", "user_id"),
        ("chats", "chat_id"),
        ("subscriptions", "chat_id, user_id"),
        ("outbox", "event_id"),
    ]:
        assert _dump(src, table, order_by) == _dump(dest, table, order_by), table


# --- backup: safety while the bot is "live" (WAL + concurrent writer) ---


def test_backup_is_consistent_with_writer_holding_uncommitted_transaction(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)

    # a separate connection (standing in for the running bot process) opens a
    # write transaction and leaves it open, uncommitted, while backup runs
    writer = sqlite3.connect(src)
    writer.execute("PRAGMA busy_timeout=5000")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO users(user_id, display_name, updated_at) VALUES "
        "(999, 'Ghost', '2026-01-01T00:00:00+00:00')"
    )

    dest = str(tmp_path / "backup.sqlite3")
    try:
        rc = cli.main(["backup", dest])
    finally:
        writer.rollback()
        writer.close()

    assert rc == 0
    assert cli.main(["verify", dest]) == 0

    check = sqlite3.connect(dest)
    try:
        ids = [r[0] for r in check.execute("SELECT user_id FROM users").fetchall()]
    finally:
        check.close()
    assert 999 not in ids  # uncommitted insert never observed
    assert {1, 2, 3, 4} <= set(ids)  # the committed baseline is intact


def test_backup_includes_committed_wal_content_not_yet_checkpointed(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, lambda c: Services().touch_user(c, 1, display_name="Root"))

    # a bystander connection stays open so that the writer's own close below is
    # not "the last connection" (which would otherwise auto-checkpoint the WAL)
    bystander = sqlite3.connect(src)
    bystander.execute("SELECT COUNT(*) FROM users").fetchone()

    try:
        writer = sqlite3.connect(src)
        writer.execute("PRAGMA busy_timeout=5000")
        writer.execute(
            "INSERT INTO users(user_id, display_name, updated_at) VALUES "
            "(2, 'Bob', '2026-01-01T00:00:00+00:00')"
        )
        writer.commit()
        writer.close()

        wal_path = Path(src + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0

        # a naive copy of only the main file misses data still sitting in -wal
        naive_copy = tmp_path / "naive.sqlite3"
        shutil.copyfile(src, naive_copy)
        naive_conn = sqlite3.connect(str(naive_copy))
        try:
            naive_ids = [r[0] for r in naive_conn.execute("SELECT user_id FROM users").fetchall()]
        finally:
            naive_conn.close()
        assert 2 not in naive_ids

        dest = str(tmp_path / "backup.sqlite3")
        assert cli.main(["backup", dest]) == 0
    finally:
        bystander.close()

    check = sqlite3.connect(dest)
    try:
        ids = [r[0] for r in check.execute("SELECT user_id FROM users").fetchall()]
    finally:
        check.close()
    assert 2 in ids  # the backup API merges committed WAL content correctly


# --- verify ---


def test_verify_accepts_a_good_backup(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)

    dest = str(tmp_path / "backup.sqlite3")
    assert cli.main(["backup", dest]) == 0
    assert cli.main(["verify", dest]) == 0


def test_verify_leaves_no_files_behind(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)
    outdir = tmp_path / "out"
    dest = outdir / "backup.sqlite3"
    assert cli.main(["backup", str(dest)]) == 0

    before = {p.name: p.read_bytes() for p in outdir.iterdir()}
    assert cli.main(["verify", str(dest)]) == 0
    assert {p.name: p.read_bytes() for p in outdir.iterdir()} == before


def test_verify_works_on_read_only_directory(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)
    outdir = tmp_path / "ro"
    dest = outdir / "backup.sqlite3"
    assert cli.main(["backup", str(dest)]) == 0
    outdir.chmod(0o500)
    try:
        assert cli.main(["verify", str(dest)]) == 0
    finally:
        outdir.chmod(0o700)


def test_verify_rejects_truncated_file(tmp_path, monkeypatch):
    src = _db_path(tmp_path)
    monkeypatch.setenv("UPB_DB_PATH", src)
    _write(src, _seed_full_state)

    dest = tmp_path / "backup.sqlite3"
    assert cli.main(["backup", str(dest)]) == 0

    data = dest.read_bytes()
    dest.write_bytes(data[:100])  # header survives, every real page is gone

    assert cli.main(["verify", str(dest)]) != 0


def test_verify_rejects_non_database_file(tmp_path):
    junk = tmp_path / "not_a_db.sqlite3"
    junk.write_text("this is not a sqlite database, just plain text\n" * 20)

    assert cli.main(["verify", str(junk)]) != 0


def test_verify_rejects_missing_file(tmp_path):
    missing = tmp_path / "nope.sqlite3"
    assert cli.main(["verify", str(missing)]) != 0


def test_verify_rejects_empty_file_missing_schema(tmp_path):
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")  # a fresh, valid-but-schemaless sqlite file

    assert cli.main(["verify", str(empty)]) != 0
