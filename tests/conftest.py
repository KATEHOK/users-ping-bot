from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.clock import Clock
from app.db import Database, apply_migrations
from app.models import IncomingEvent


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
