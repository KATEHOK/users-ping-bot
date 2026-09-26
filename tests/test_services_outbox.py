import pytest

from app.db import Database
from app.services import Services


async def _register(services, db, user_id, chat_id, title="Chat"):
    async with db.transaction() as c:
        await services.touch_user(c, user_id)
        return await services.register_chat(c, chat_id, title, user_id)


async def test_reregistering_before_old_farewell_delivered_cancels_stale_event(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.unregister_chat(c, -100)

    async with db.reader() as c:
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert len(events) == 1
    stale_event_id = events[0].event_id

    async with db.transaction() as c:
        await services.register_chat(c, -100, "Chat", 1)

    async with db.reader() as c:
        assert await services.event_status(c, stale_event_id) == "cancelled"


async def test_g1_unregister_g2_register_g2_unregister_leaves_only_g2_deliverable(
    db: Database,
):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.unregister_chat(c, -100)  # G1 farewell queued

    async with db.transaction() as c:
        await services.register_chat(c, -100, "Chat", 1)  # G2: cancels G1 farewell

    async with db.transaction() as c:
        await services.unregister_chat(c, -100)  # G2 farewell queued

    async with db.reader() as c:
        assert await services.get_chat(c, -100) is None  # no chats row left
        pending = await services.due_events(c, "9999-01-01T00:00:00+00:00")

    assert len(pending) == 1
    assert pending[0].generation == 2
    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT status, generation FROM outbox WHERE event_type = 'chat_farewell' "
            "ORDER BY generation"
        )
        rows = await cursor.fetchall()
    assert rows == [("cancelled", 1), ("pending", 2)]


async def test_queue_event_deduplicates_by_key(db: Database):
    services = Services()
    async with db.transaction() as c:
        first = await services.queue_event(
            c,
            event_key="k1",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=-1,
            generation=1,
            payload={},
        )
        second = await services.queue_event(
            c,
            event_key="k1",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=-1,
            generation=1,
            payload={},
        )
    assert first is not None
    assert second is None


async def test_event_status_reflects_cancellation_after_worker_fetched_it(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.unregister_chat(c, -100)

    async with db.reader() as c:
        job = (await services.due_events(c, "9999-01-01T00:00:00+00:00"))[0]

    # something else cancels the chat's events between fetch and send attempt
    async with db.transaction() as c:
        await services.cancel_chat_events(c, -100)

    async with db.reader() as c:
        status = await services.event_status(c, job.event_id)
    assert status == "cancelled"  # locally held job is not permission to send


async def test_due_events_orders_oldest_first_and_respects_limit(db: Database):
    services = Services()
    async with db.transaction() as c:
        for i in range(3):
            await services.queue_event(
                c,
                event_key=f"k{i}",
                event_type="chat_farewell",
                target_kind="chat",
                target_id=-i,
                generation=1,
                payload={},
            )
    async with db.reader() as c:
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00", limit=2)
    assert [e.event_key for e in events] == ["k0", "k1"]


async def test_due_events_respects_next_attempt_at(db: Database):
    services = Services()
    async with db.transaction() as c:
        event_id = await services.queue_event(
            c,
            event_key="k",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=-1,
            generation=1,
            payload={},
        )
        await services.mark_event(
            c, event_id, "pending", error="rate_limited", retry_at="2030-01-01T00:00:00+00:00"
        )

    async with db.reader() as c:
        too_early = await services.due_events(c, "2020-01-01T00:00:00+00:00")
        ready = await services.due_events(c, "2031-01-01T00:00:00+00:00")
    assert too_early == []
    assert len(ready) == 1
    assert ready[0].attempts == 1
    assert ready[0].last_error == "rate_limited"


async def test_mark_event_sent_is_not_due_again(db: Database):
    services = Services()
    async with db.transaction() as c:
        event_id = await services.queue_event(
            c,
            event_key="k",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=-1,
            generation=1,
            payload={},
        )
        await services.mark_event(c, event_id, "sent")
    async with db.reader() as c:
        assert await services.due_events(c, "9999-01-01T00:00:00+00:00") == []
        assert await services.event_status(c, event_id) == "sent"


async def test_cancel_chat_events_leaves_sent_events_untouched(db: Database):
    services = Services()
    async with db.transaction() as c:
        sent_id = await services.queue_event(
            c,
            event_key="sent-one",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=-100,
            generation=1,
            payload={},
        )
        await services.mark_event(c, sent_id, "sent")
        pending_id = await services.queue_event(
            c,
            event_key="pending-one",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=-100,
            generation=2,
            payload={},
        )

    async with db.transaction() as c:
        cancelled = await services.cancel_chat_events(c, -100)

    assert cancelled == 1
    async with db.reader() as c:
        assert await services.event_status(c, sent_id) == "sent"
        assert await services.event_status(c, pending_id) == "cancelled"


async def test_root_revoked_event_queued_only_with_prior_private_contact(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1, private_contact=True)
        await services.set_root(c, 1)
    async with db.transaction() as c:
        result = await services.set_root(c, 2)
    assert result.notified_previous is True

    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT target_kind, target_id, status FROM outbox WHERE event_type = 'root_revoked'"
        )
        rows = await cursor.fetchall()
    assert rows == [("user", 1, "pending")]


async def test_no_root_revoked_event_without_prior_contact(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.set_root(c, 1)
    async with db.transaction() as c:
        result = await services.set_root(c, 2)
    assert result.notified_previous is False

    async with db.reader() as c:
        cursor = await c.execute("SELECT COUNT(*) FROM outbox WHERE event_type = 'root_revoked'")
        (count,) = await cursor.fetchone()
    assert count == 0
