import asyncio
import os
import subprocess
import sys
import threading
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


def _read(db_path: str, fn):
    async def runner():
        async with open_database(db_path) as db:
            async with db.reader() as c:
                return await fn(c)

    return asyncio.run(runner())


def _db_path(tmp_path) -> str:
    return str(tmp_path / "cli.sqlite3")


@pytest.fixture(autouse=True)
def _isolated_db_path(monkeypatch, tmp_path):
    # every test gets its own UPB_DB_PATH so cli.main() never touches a shared file
    monkeypatch.setenv("UPB_DB_PATH", _db_path(tmp_path))


# --- set-root ---


def test_set_root_creates_root_on_empty_database(tmp_path):
    path = _db_path(tmp_path)
    assert cli.main(["set-root", "42"]) == 0

    root = _read(path, lambda c: Services().get_root(c))
    assert root == 42


def test_set_root_same_id_is_noop(tmp_path, capsys):
    path = _db_path(tmp_path)
    assert cli.main(["set-root", "42"]) == 0
    capsys.readouterr()

    assert cli.main(["set-root", "42"]) == 0
    out = capsys.readouterr().out
    assert "changed=no" in out
    assert "registrations_dropped=0" in out

    async def _check(c):
        services = Services()
        assert await services.get_root(c) == 42
        chats = await services.list_chats(c)
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00")
        return chats, events

    chats, events = _read(path, _check)
    assert chats == []
    assert events == []


def test_set_root_promotes_admin_keeps_chats_drops_previous_root(tmp_path, capsys):
    path = _db_path(tmp_path)

    async def setup(c):
        services = Services()
        await services.set_root(c, 1)  # A becomes root
        await services.touch_user(c, 1, private_contact=True)  # A has a private contact
        await services.register_chat(c, -200, "A's chat", 1)
        await services.touch_user(c, 2)
        await services.grant_admin(c, 2)  # B becomes admin
        await services.register_chat(c, -100, "B's chat", 2)
        await services.touch_user(c, 5)
        await services.subscribe(c, -100, 5)

    _write(path, setup)

    assert cli.main(["set-root", "2"]) == 0
    out = capsys.readouterr().out
    assert "previous_root=1" in out
    assert "new_root=2" in out
    assert "registrations_dropped=1" in out
    assert "previous_notified=yes" in out

    async def _check(c):
        services = Services()
        root = await services.get_root(c)
        role_a = await services.get_role(c, 1)
        chat_b = await services.get_chat(c, -100)
        chat_a = await services.get_chat(c, -200)
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00", limit=100)
        return root, role_a, chat_b, chat_a, events

    root, role_a, chat_b, chat_a, events = _read(path, _check)
    assert root == 2
    assert role_a is None
    assert chat_b is not None and chat_b.registered_by == 2  # B's chat kept
    assert chat_a is None  # A's chat dropped

    event_types = sorted(e.event_type for e in events)
    assert event_types == ["chat_farewell", "root_revoked"]
    revoked = next(e for e in events if e.event_type == "root_revoked")
    assert revoked.target_kind == "user"
    assert revoked.target_id == 1


def test_set_root_no_notification_without_prior_private_contact(tmp_path):
    path = _db_path(tmp_path)
    _write(path, lambda c: Services().set_root(c, 1))

    assert cli.main(["set-root", "2"]) == 0

    async def _check(c):
        services = Services()
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00", limit=100)
        return events

    events = _read(path, _check)
    assert [e.event_type for e in events] == []


def test_two_sequential_set_root_runs_never_leave_two_roots(tmp_path):
    path = _db_path(tmp_path)
    assert cli.main(["set-root", "10"]) == 0
    assert cli.main(["set-root", "20"]) == 0

    async def _check(c):
        cursor = await c.execute("SELECT user_id FROM roles WHERE role = 'root'")
        return await cursor.fetchall()

    rows = _read(path, _check)
    assert rows == [(20,)]


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_set_root_invalid_id_exits_2_and_writes_nothing(tmp_path, bad):
    path = _db_path(tmp_path)
    assert cli.main(["set-root", bad]) == 2
    assert not Path(path).exists()


def test_set_root_missing_id_exits_2_and_writes_nothing(tmp_path):
    path = _db_path(tmp_path)
    assert cli.main(["set-root"]) == 2
    assert not Path(path).exists()


def test_cli_applies_migrations_on_database_bot_never_opened(tmp_path):
    path = _db_path(tmp_path)
    assert not Path(path).exists()
    assert cli.main(["set-root", "7"]) == 0
    assert Path(path).exists()

    async def _check(c):
        cursor = await c.execute("SELECT version FROM schema_migrations")
        return await cursor.fetchall()

    rows = _read(path, _check)
    assert len(rows) >= 1


def test_migration_conflicts_command_is_gone(capsys):
    assert cli.main(["migration-conflicts", "list"]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_does_not_import_bot_stack(tmp_path):
    db_path = tmp_path / "iso.sqlite3"
    script = (
        "import sys\n"
        "from app import cli\n"
        "rc = cli.main(['set-root', '1'])\n"
        "forbidden_prefixes = ('app.vault', 'app.telegram', 'app.__main__', 'aiogram')\n"
        "hit = sorted(m for m in sys.modules if m.startswith(forbidden_prefixes))\n"
        "print('RC=%d' % rc)\n"
        "print('HIT=' + ','.join(hit))\n"
    )
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["UPB_DB_PATH"] = str(db_path)
    env["PYTHONPATH"] = src_dir
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    assert "HIT=" in result.stdout
    hit_line = next(line for line in result.stdout.splitlines() if line.startswith("HIT="))
    assert hit_line == "HIT=", f"forbidden modules imported: {hit_line}"
