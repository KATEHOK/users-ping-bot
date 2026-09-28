import asyncio
import re

import pytest

from app import rendering
from app.clock import iso
from app.delivery import (
    AmbiguousSend,
    ChatMigrated,
    Delivery,
    PermanentSend,
    RateLimited,
    Unauthorized,
)
from app.models import SubscriberRef
from app.services import Services

from conftest import FakeClock, RecordingTransport, register_chat

CHAT = -500
MSG = 7
THREAD = 3


def _refs(n: int, start: int = 1000) -> list[SubscriberRef]:
    return [SubscriberRef(user_id=start + i, display_name=f"User{start + i}") for i in range(n)]


def _mk(db, clock=None, stop=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db, services, transport, clock=clock, stop=stop)
    return delivery, services, transport, clock


def _ids(text: str) -> list[int]:
    return [int(m) for m in re.findall(r'tg://user\?id=(\d+)"', text)]


class BlockingClock(FakeClock):
    """sleep() really suspends: only a stop event can end the wait."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await asyncio.Event().wait()


async def test_chunks_are_capped_at_fifty_mentions_and_reply_to_the_command(db):
    delivery, _s, transport, _c = _mk(db)
    await delivery.run_ping(CHAT, MSG, THREAD, _refs(120))
    assert [len(_ids(c["text"])) for c in transport.calls] == [50, 50, 20]
    assert all(c["reply_to_message_id"] == MSG and c["thread_id"] == THREAD for c in transport.calls)
    assert [i for c in transport.calls for i in _ids(c["text"])] == [1000 + i for i in range(120)]


async def test_chunks_respect_the_character_limit(db):
    delivery, _s, transport, _c = _mk(db)
    refs = [SubscriberRef(user_id=i + 1, display_name="N" * 200) for i in range(40)]
    await delivery.run_ping(CHAT, MSG, None, refs)
    assert len(transport.calls) > 1
    assert all(len(c["text"]) <= rendering.MAX_MESSAGE for c in transport.calls)
    assert sum(len(_ids(c["text"])) for c in transport.calls) == 40


async def test_mention_uses_id_link_and_fallback_label(db):
    delivery, _s, transport, _c = _mk(db)
    await delivery.run_ping(
        CHAT, MSG, None, [SubscriberRef(user_id=5, display_name="A<b>&"), SubscriberRef(user_id=6)]
    )
    text = transport.calls[0]["text"]
    assert '<a href="tg://user?id=5">A&lt;b&gt;&amp;</a>' in text
    assert '<a href="tg://user?id=6">id6</a>' in text


async def test_empty_snapshot_answers_pong(db):
    delivery, _s, transport, _c = _mk(db)
    await delivery.run_ping(CHAT, MSG, THREAD, [])
    assert [c["text"] for c in transport.calls] == ["pong"]
    assert transport.calls[0]["reply_to_message_id"] == MSG


async def test_rate_limit_is_retried_honouring_retry_after(db):
    delivery, _s, transport, clock = _mk(db)
    transport.queue_raises([RateLimited(12.0), RateLimited(7.0)])
    start = clock.now()
    await delivery.run_ping(CHAT, MSG, None, _refs(3))
    assert len(transport.calls) == 3  # two 429s, then success
    assert (clock.now() - start).total_seconds() == 19.0


async def test_rate_limit_retries_are_capped_at_three_per_chunk(db):
    delivery, _s, transport, _c = _mk(db)
    transport.queue_raises([RateLimited(1.0)] * 10)
    await delivery.run_ping(CHAT, MSG, None, _refs(60))  # two chunks
    assert len(transport.calls) == 4  # first try + 3 retries, then the whole remainder is dropped


async def test_retry_budget_is_per_chunk(db):
    delivery, _s, transport, _c = _mk(db)
    transport.queue_raises([RateLimited(1.0)] * 3)
    await delivery.run_ping(CHAT, MSG, None, _refs(60))
    # chunk 1: 3 retries then success; chunk 2 goes through cleanly
    assert len(transport.calls) == 5


@pytest.mark.parametrize("exc", [PermanentSend(), AmbiguousSend()])
async def test_other_errors_cancel_the_remainder_and_are_logged(db, caplog, exc):
    caplog.set_level("INFO")
    delivery, _s, transport, _c = _mk(db)
    transport.queue_raises([exc])
    await delivery.run_ping(CHAT, MSG, None, _refs(120))
    assert len(transport.calls) == 1
    assert any("ping_" in r.getMessage() for r in caplog.records)


async def test_error_on_a_later_chunk_stops_only_the_rest(db):
    delivery, _s, transport, _c = _mk(db)
    calls = {"n": 0}
    original = transport.send_message

    async def flaky(chat_id, text, **kw):
        calls["n"] += 1
        await original(chat_id, text, **kw)
        if calls["n"] == 2:
            raise PermanentSend()

    transport.send_message = flaky
    await delivery.run_ping(CHAT, MSG, None, _refs(120))
    assert calls["n"] == 2


async def test_unauthorized_is_fatal_and_propagates(db):
    delivery, _s, transport, _c = _mk(db)
    transport.queue_raises([Unauthorized()])
    with pytest.raises(Unauthorized):
        await delivery.run_ping(CHAT, MSG, None, _refs(3))


async def test_send_reply_never_raises_send_errors(db):
    delivery, _s, transport, _c = _mk(db)
    for exc in (PermanentSend(), AmbiguousSend(), RateLimited(1.0)):
        transport.queue_raises([exc] * 10)
        assert await delivery.send_reply(CHAT, "x", reply_to=1, thread_id=None) is False
        transport._raise_queue.clear()
    assert await delivery.send_reply(CHAT, "x", reply_to=1, thread_id=None) is True


async def test_migrated_chat_applies_migration_and_cancels_the_ping(db):
    delivery, services, transport, _c = _mk(db)
    await register_chat(db, services, CHAT, 1)
    transport.queue_raises([ChatMigrated(-1000)])
    await delivery.run_ping(CHAT, MSG, None, _refs(120))
    assert len(transport.calls) == 1  # nothing after the migration answer
    async with db.reader() as c:
        assert await services.get_chat(c, CHAT) is None
        assert await services.get_chat(c, -1000) is not None
        assert await services.resolve_chat_id(c, CHAT) == -1000


async def test_rate_limit_wait_is_interrupted_by_stop(db):
    clock = BlockingClock()
    stop = asyncio.Event()
    delivery, _s, transport, _c = _mk(db, clock, stop)
    transport.queue_raises([RateLimited(3600.0)])
    task = asyncio.create_task(delivery.run_ping(CHAT, MSG, None, _refs(3)))
    await asyncio.wait_for(clock.sleeping.wait(), 2)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(transport.calls) == 1  # no retry after the stop
