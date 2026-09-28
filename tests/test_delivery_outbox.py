import asyncio
import logging
from datetime import timedelta

import pytest

from app.clock import iso
from app.delivery import (
    OUTBOX_MAX_ATTEMPTS,
    AmbiguousSend,
    ChatMigrated,
    Delivery,
    PermanentSend,
    RateLimited,
    Unauthorized,
)
from app.services import Services

from conftest import FakeClock, RecordingTransport, make_root, register_chat

CHAT = -500


def _mk(db, clock=None, stop=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db, services, transport, clock=clock, stop=stop)
    return delivery, services, transport, clock


async def _queue(db, services, *, key="k1", etype="chat_farewell", target=CHAT, kind="chat", payload=None):
    async with db.transaction() as c:
        return await services.queue_event(
            c,
            event_key=key,
            event_type=etype,
            target_kind=kind,
            target_id=target,
            generation=1,
            payload=payload if payload is not None else {"lang": "en"},
        )


async def _event(db, services, event_id):
    async with db.reader() as c:
        return await services.get_event(c, event_id)


@pytest.mark.parametrize("lang,text", [("en", "Chat unregistered. Bye!"), ("ru", "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0447\u0430\u0442\u0430 \u0441\u043d\u044f\u0442\u0430. \u0414\u043e \u0432\u0441\u0442\u0440\u0435\u0447\u0438!")])
async def test_farewell_is_sent_in_the_payload_language(db, lang, text):
    delivery, services, transport, _c = _mk(db)
    eid = await _queue(db, services, payload={"lang": lang})
    assert await delivery.run_outbox_once() == 1
    assert [(c["chat_id"], c["text"], c["reply_to_message_id"]) for c in transport.calls] == [
        (CHAT, text, None)
    ]
    assert (await _event(db, services, eid)).status == "sent"


async def test_unregister_through_services_carries_the_chat_language(db):
    delivery, services, transport, _c = _mk(db)
    await register_chat(db, services, CHAT, 1)
    async with db.transaction() as c:
        await services.set_chat_lang(c, CHAT, "ru")
        await services.unregister_chat(c, CHAT)
    await delivery.run_outbox_once()
    assert transport.calls[0]["text"] == "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0447\u0430\u0442\u0430 \u0441\u043d\u044f\u0442\u0430. \u0414\u043e \u0432\u0441\u0442\u0440\u0435\u0447\u0438!"


@pytest.mark.parametrize("lang", ["en", "ru"])
async def test_root_revoked_names_the_event_time_in_the_payload_language(db, lang):
    delivery, services, transport, _c = _mk(db)
    eid = await _queue(
        db, services, key="r", etype="root_revoked", target=42, kind="user", payload={"lang": lang}
    )
    await delivery.run_outbox_once()
    created = (await _event(db, services, eid)).created_at
    expected = f"Root role revoked at {created}." if lang == "en" else f"\u0420\u043e\u043b\u044c root \u0441\u043d\u044f\u0442\u0430: {created}."
    assert transport.calls[0]["text"] == expected
    assert transport.calls[0]["chat_id"] == 42


async def test_set_root_queues_a_notice_in_the_users_language(db):
    delivery, services, transport, _c = _mk(db)
    await make_root(db, services, 1, contact=True)
    async with db.transaction() as c:
        await services.set_user_lang(c, 1, "ru")
        await services.touch_user(c, 2)
        await services.set_root(c, 2)
    await delivery.run_outbox_once()
    assert transport.calls[0]["chat_id"] == 1
    assert transport.calls[0]["text"].startswith("\u0420\u043e\u043b\u044c root \u0441\u043d\u044f\u0442\u0430:")


async def test_whole_row_is_reread_before_the_attempt(db):
    delivery, services, transport, _c = _mk(db)
    eid = await _queue(db, services)
    async with db.reader() as c:
        stale = await services.due_events(c, iso(FakeClock().now()))
    async with db.transaction() as c:
        await services.cancel_chat_events(c, CHAT)
    assert stale[0].status == "pending"
    assert await delivery._deliver_one(stale[0]) is False
    assert transport.calls == []
    assert (await _event(db, services, eid)).status == "cancelled"


async def test_retargeted_row_is_sent_to_the_current_target(db):
    delivery, services, transport, _c = _mk(db)
    await _queue(db, services)
    async with db.reader() as c:
        stale = await services.due_events(c, iso(FakeClock().now()))
    async with db.transaction() as c:
        await services.migrate_chat(c, CHAT, -1000)
    await delivery._deliver_one(stale[0])
    assert transport.calls[0]["chat_id"] == -1000


async def test_three_attempts_then_failed_with_a_log_line(db, caplog):
    delivery, services, transport, clock = _mk(db)
    eid = await _queue(db, services)
    transport.queue_raises([AmbiguousSend()] * 10)
    with caplog.at_level(logging.ERROR):
        for _ in range(6):
            await delivery.run_outbox_once()
            clock.advance(4000)
    ev = await _event(db, services, eid)
    assert (ev.status, ev.attempts) == ("failed", OUTBOX_MAX_ATTEMPTS)
    assert len(transport.calls) == 3
    assert any("outbox_failed" in r.getMessage() and f"event_id={eid}" in r.getMessage() for r in caplog.records)


async def test_permanent_error_fails_at_once_and_logs(db, caplog):
    delivery, services, transport, _c = _mk(db)
    eid = await _queue(db, services)
    transport.queue_raises([PermanentSend()])
    with caplog.at_level(logging.ERROR):
        await delivery.run_outbox_once()
    ev = await _event(db, services, eid)
    assert (ev.status, ev.attempts, ev.last_error) == ("failed", 1, "permanent")
    assert any("outbox_failed" in r.getMessage() for r in caplog.records)


async def test_rate_limit_honours_retry_after(db):
    delivery, services, transport, clock = _mk(db)
    eid = await _queue(db, services)
    transport.queue_raises([RateLimited(600.0)])
    await delivery.run_outbox_once()
    ev = await _event(db, services, eid)
    assert ev.status == "pending" and ev.attempts == 1
    assert ev.next_attempt_at == iso(clock.now() + timedelta(seconds=600))
    assert await delivery.run_outbox_once() == 0  # not due yet
    clock.advance(601)
    assert await delivery.run_outbox_once() == 1


async def test_chat_migrated_retargets_and_retries_on_the_new_id(db):
    delivery, services, transport, _c = _mk(db)
    eid = await _queue(db, services)
    transport.fail_chat(CHAT, ChatMigrated(-1000))
    await delivery.run_outbox_once()
    ev = await _event(db, services, eid)
    assert (ev.status, ev.target_id) == ("pending", -1000)
    async with db.reader() as c:
        assert await services.resolve_chat_id(c, CHAT) == -1000
    await delivery.run_outbox_once()  # next cycle
    assert [c["chat_id"] for c in transport.calls] == [CHAT, -1000]
    assert (await _event(db, services, eid)).status == "sent"


async def test_contradictory_migration_cannot_loop_forever(db):
    delivery, services, transport, clock = _mk(db)
    eid = await _queue(db, services)
    async with db.transaction() as c:
        await services.migrate_chat(c, CHAT, -7)
    # the row was retargeted to -7; force it back to simulate a stuck target
    async with db.transaction() as c:
        await c.execute("UPDATE outbox SET target_id = ?", (CHAT,))
    transport.fail_chat(CHAT, ChatMigrated(-8))
    for _ in range(6):
        await delivery.run_outbox_once()
        clock.advance(10)
    assert (await _event(db, services, eid)).status == "failed"
    assert len(transport.calls) == 3


async def test_unknown_event_type_is_failed_without_a_send(db):
    delivery, services, transport, _c = _mk(db)
    eid = await _queue(db, services, etype="mystery")
    await delivery.run_outbox_once()
    assert transport.calls == []
    assert (await _event(db, services, eid)).status == "failed"


async def test_one_bad_event_does_not_stop_the_batch(db, monkeypatch):
    delivery, services, transport, _c = _mk(db)
    await _queue(db, services, key="a", target=-1)
    await _queue(db, services, key="b", target=-2)
    original = delivery._render_outbox_text
    n = {"i": 0}

    def flaky(event):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("boom")
        return original(event)

    monkeypatch.setattr(delivery, "_render_outbox_text", flaky)
    assert await delivery.run_outbox_once() == 1
    assert [c["chat_id"] for c in transport.calls] == [-2]


async def test_unauthorized_in_the_outbox_is_fatal(db):
    delivery, services, transport, _c = _mk(db)
    await _queue(db, services)
    transport.queue_raises([Unauthorized()])
    with pytest.raises(Unauthorized):
        await delivery.run_outbox_once()


async def test_outbox_loop_stops_promptly_on_stop(db):
    stop = asyncio.Event()

    class Blocking(FakeClock):
        async def sleep(self, seconds):
            await asyncio.Event().wait()

    delivery, services, transport, _c = _mk(db, Blocking(), stop)
    task = asyncio.create_task(delivery.outbox_loop(stop))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)
