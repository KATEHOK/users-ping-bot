import builtins
import os
import socket as socket_module
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.clock import Clock
from app.db import Database, apply_migrations
from app.models import IncomingEvent

# Plan section 15 requires that the whole suite runs against temp databases and
# fake transports only: no production .env, no network sockets. The two guards
# below fail loudly (rather than silently letting a stray real access through)
# so a future test cannot accidentally reintroduce either dependency.

# Env vars a real production .env could set (see .env.example). No test may rely
# on these being present via plain os.environ fallback: every test either passes
# an explicit env mapping to config.load_config()/load_db_path(), or
# monkeypatch.setenv()s its own value.
_PRODUCTION_ENV_VARS = (
    "VAULT_ADDR",
    "VAULT_ROLE_ID",
    "VAULT_SECRET_ID",
    "VAULT_SECRET_PATH",
    "VAULT_AUTH_MOUNT",
    "VAULT_KV_MOUNT",
    "VAULT_CA_PATH",
    "VAULT_TIMEOUT",
    "UPB_DB_PATH",
    "UPB_LOG_LEVEL",
)

_REAL_ENV_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, ".env"))
_ORIGINAL_OPEN = builtins.open


def _guarded_open(file, *args, **kwargs):
    if isinstance(file, (str, bytes, os.PathLike)):
        if os.path.abspath(os.fspath(file)) == _REAL_ENV_FILE:
            raise AssertionError("tests must never read the repository's real .env file")
    return _ORIGINAL_OPEN(file, *args, **kwargs)


def _guarded_connect(self, address):
    # blocks only actual outbound connection attempts, never plain socket
    # construction: asyncio's own event loop wiring (e.g. its self-pipe) uses
    # a local AF_UNIX socketpair() internally (no connect() call) and keeps working
    raise AssertionError(f"tests must never open a real network connection to {address!r}")


@pytest.fixture(autouse=True)
def _no_network_no_production_env(monkeypatch):
    monkeypatch.setattr(builtins, "open", _guarded_open)
    monkeypatch.setattr(socket_module.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket_module.socket, "connect_ex", _guarded_connect)
    for name in _PRODUCTION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise: Exception | None = None
        self._raise_queue: list[Exception] = []
        self._fail_chats: dict[int, Exception] = {}

    def raise_next(self, exc: Exception) -> None:
        self._raise = exc

    def queue_raises(self, excs: list[Exception]) -> None:
        # each call pops the next queued exception, in order, until exhausted
        self._raise_queue.extend(excs)

    def fail_chat(self, chat_id: int, exc: Exception) -> None:
        # every send to this chat_id raises exc until clear_fail_chat is called
        self._fail_chats[chat_id] = exc

    def clear_fail_chat(self, chat_id: int) -> None:
        self._fail_chats.pop(chat_id, None)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        self.calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "thread_id": thread_id,
            }
        )
        if chat_id in self._fail_chats:
            raise self._fail_chats[chat_id]
        if self._raise_queue:
            raise self._raise_queue.pop(0)
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc


def make_event(**kwargs: Any) -> IncomingEvent:
    defaults: dict[str, Any] = dict(
        kind="message",
        update_id=1,
        chat_id=100,
        chat_type="group",
        user_id=1000,
        username="user",
        display_name="User",
        message_id=1,
        text="",
    )
    defaults.update(kwargs)
    return IncomingEvent(**defaults)


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    database = Database(path)
    await database.connect()
    await apply_migrations(database)
    try:
        yield database
    finally:
        await database.close()
