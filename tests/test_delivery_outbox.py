import logging

import pytest

from app.clock import iso
from app.db import Database, open_database
from app.delivery import AmbiguousSend, Delivery, PermanentSend, RateLimited
from app.services import Services

from conftest import FakeClock, RecordingTransport

CHAT_A = 300
CHAT_B = 301


def _mk_delivery(db, clock=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    return Delivery(db, services, transport, clock=clock), services, transport, clock


@pytest.mark.asyncio
async def test_farewell_is_delivered_and_marked_sent(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        result = await services.unregister_chat(c, CHAT_A)

    delivered = await delivery.run_outbox_once()

    assert delivered == 1
    assert len(transport.calls) == 1
    assert transport.calls[0]["chat_id"] == CHAT_A
    assert transport.calls[0]["reply_to_message_id"] is None  # standalone, no reply
    async with db.reader() as c:
        rows = await services.due_events(c, iso(clock.now()))
    assert rows == []  # sent event is no longer due


@pytest.mark.asyncio
async def test_root_revoked_notice_carries_event_time(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 10, private_contact=True)
        await services.set_root(c, 10)
    clock.advance(3600)
    async with db.transaction() as c:
        # setting a new root queues a root_revoked notice for the old one
        await services.set_root(c, 11)

    await delivery.run_outbox_once()

    assert len(transport.calls) == 1
    assert transport.calls[0]["chat_id"] == 10
    assert transport.calls[0]["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_skips_event_cancelled_after_it_was_fetched(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        await services.unregister_chat(c, CHAT_A)
        # re-registering cancels the pending farewell of the previous generation
        await services.register_chat(c, CHAT_A, "Chat A", 1)

    delivered = await delivery.run_outbox_once()

    assert delivered == 0
    assert transport.calls == []


@pytest.mark.asyncio
async def test_one_unreachable_chat_does_not_block_others(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        await services.register_chat(c, CHAT_B, "Chat B", 1)
        await services.unregister_chat(c, CHAT_A)
        await services.unregister_chat(c, CHAT_B)

    transport.fail_chat(CHAT_A, PermanentSend())

    delivered = await delivery.run_outbox_once()

    assert delivered == 1  # CHAT_B still got its farewell
    chat_ids_sent = {call["chat_id"] for call in transport.calls}
    assert CHAT_A in chat_ids_sent
    assert CHAT_B in chat_ids_sent


@pytest.mark.asyncio
async def test_permanent_failure_ends_event_as_failed(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        result = await services.unregister_chat(c, CHAT_A)

    transport.fail_chat(CHAT_A, PermanentSend())
    delivered = await delivery.run_outbox_once()

    assert delivered == 0
    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT status, last_error FROM outbox WHERE target_kind='chat' AND target_id=?",
            (CHAT_A,),
        )
        status, last_error = await cursor.fetchone()
    assert status == "failed"
    assert last_error == "permanent"


@pytest.mark.asyncio
async def test_temporary_failure_is_retried_with_backoff(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        await services.unregister_chat(c, CHAT_A)

    transport.raise_next(RateLimited(retry_after=5.0))
    delivered = await delivery.run_outbox_once()
    assert delivered == 0

    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT status, attempts, next_attempt_at, last_error FROM outbox "
            "WHERE target_kind='chat' AND target_id=?",
            (CHAT_A,),
        )
        status, attempts, next_attempt_at, last_error = await cursor.fetchone()
    assert status == "pending"
    assert attempts == 1
    assert next_attempt_at is not None
    assert next_attempt_at > iso(clock.now())
    assert last_error == "rate_limited"

    # not due yet: a run right now must not retry it
    delivered_again = await delivery.run_outbox_once()
    assert delivered_again == 0
    assert len(transport.calls) == 1

    # advance past next_attempt_at and it becomes due again
    clock.advance(3600)
    delivered_final = await delivery.run_outbox_once()
    assert delivered_final == 1
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_ambiguous_outbox_failure_is_retried_as_temporary(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        await services.unregister_chat(c, CHAT_A)

    transport.raise_next(AmbiguousSend())
    delivered = await delivery.run_outbox_once()
    assert delivered == 0

    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT status, last_error FROM outbox WHERE target_kind='chat' AND target_id=?",
            (CHAT_A,),
        )
        status, last_error = await cursor.fetchone()
    assert status == "pending"
    assert last_error == "ambiguous"


@pytest.mark.asyncio
async def test_unexpected_error_in_one_event_does_not_stop_the_batch(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        await services.register_chat(c, CHAT_B, "Chat B", 1)
        await services.unregister_chat(c, CHAT_A)
        await services.unregister_chat(c, CHAT_B)

    transport.fail_chat(CHAT_A, RuntimeError("boom"))

    delivered = await delivery.run_outbox_once()

    assert delivered == 1  # CHAT_B still delivered despite CHAT_A raising unexpectedly
    chat_ids_sent = {call["chat_id"] for call in transport.calls}
    assert CHAT_B in chat_ids_sent


@pytest.mark.asyncio
async def test_root_revoked_permanent_failure_leaves_role_change_untouched(db):
    # punch-list P2: a root_revoked notice hits a PermanentSend (the demoted
    # root blocked the bot); the role change that queued the notice must stand
    # regardless -- the outbox worker only ever calls services.mark_event, it
    # never re-reads or reverts role state on any delivery outcome.
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 10, private_contact=True)
        await services.set_root(c, 10)
    async with db.transaction() as c:
        await services.set_root(c, 11)  # queues a root_revoked notice for user 10

    transport.fail_chat(10, PermanentSend())
    delivered = await delivery.run_outbox_once()

    assert delivered == 0
    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT status, last_error FROM outbox WHERE event_type = 'root_revoked'"
        )
        status, last_error = await cursor.fetchone()
        root = await services.get_root(c)
        role_10 = await services.get_role(c, 10)
    assert status == "failed"
    assert last_error == "permanent"
    assert root == 11  # new root stands
    assert role_10 is None  # demoted root stays demoted; nothing was rolled back


@pytest.mark.asyncio
async def test_run_outbox_once_skips_event_cancelled_between_fetch_and_final_check(tmp_path):
    # punch-list P3: drives Delivery.run_outbox_once() itself (not just the
    # services-level status transition) through fetch-then-cancel, using a
    # genuinely separate connection for the cancelling write.
    path = str(tmp_path / "cancel_race.sqlite3")
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            svc = Services()
            await svc.touch_user(c, 1)
            await svc.register_chat(c, CHAT_A, "Chat A", 1)
            await svc.unregister_chat(c, CHAT_A)  # queues a pending farewell

    db_worker = Database(path)
    db_canceller = Database(path)
    await db_worker.connect()
    await db_canceller.connect()
    clock = FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db_worker, services, transport, clock=clock)

    real_due_events = services.due_events
    cancelled_event_id: list[int] = []

    async def due_events_then_race_cancel(c, now_iso, limit=10):
        events = await real_due_events(c, now_iso, limit=limit)
        assert len(events) == 1  # exactly the farewell the worker was meant to fetch
        cancelled_event_id.append(events[0].event_id)
        # a second, independent connection cancels the event right after the
        # worker fetched it -- before the worker's own per-event re-check
        async with db_canceller.transaction() as c2:
            await Services(clock=clock).cancel_chat_events(c2, CHAT_A)
        return events

    services.due_events = due_events_then_race_cancel

    try:
        delivered = await delivery.run_outbox_once()
    finally:
        await db_worker.close()
        await db_canceller.close()

    assert delivered == 0
    assert transport.calls == []  # zero transport calls for the cancelled event

    check = Database(path)
    await check.connect()
    try:
        async with check.reader() as c:
            status = await services.event_status(c, cancelled_event_id[0])
    finally:
        await check.close()
    assert status == "cancelled"  # stays cancelled; the worker's re-check never flips it


@pytest.mark.asyncio
async def test_no_fake_token_marker_leaks_through_outbox(db, caplog):
    caplog.set_level(logging.INFO, logger="app.delivery")
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.register_chat(c, CHAT_A, "Chat A", 1)
        await services.unregister_chat(c, CHAT_A)

    fake_token = "FAKE-TOKEN-MARKER-77bb"
    transport.raise_next(PermanentSend(f"response body contained {fake_token}"))

    await delivery.run_outbox_once()

    for call in transport.calls:
        assert fake_token not in call["text"]
    for record in caplog.records:
        assert fake_token not in record.getMessage()
    async with db.reader() as c:
        cursor = await c.execute("SELECT last_error FROM outbox WHERE target_id=?", (CHAT_A,))
        (last_error,) = await cursor.fetchone()
    assert fake_token not in (last_error or "")
