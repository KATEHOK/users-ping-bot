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
        assert (await services.get_event(c, stale_event_id)).status == "cancelled"


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
        event = await services.get_event(c, job.event_id)
    assert event.status == "cancelled"  # locally held job is not permission to send


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
        assert (await services.get_event(c, event_id)).status == "sent"


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
        assert (await services.get_event(c, sent_id)).status == "sent"
        assert (await services.get_event(c, pending_id)).status == "cancelled"


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


async def test_get_event_rereads_whole_row(db: Database):
    services = Services()
    async with db.transaction() as c:
        event_id = await services.queue_event(
            c, event_key="k", event_type="chat_farewell", target_kind="chat",
            target_id=-1, generation=1, payload={"lang": "ru"},
        )
    async with db.reader() as c:
        before = await services.get_event(c, event_id)
    async with db.transaction() as c:
        await c.execute("UPDATE outbox SET target_id = -2 WHERE event_id = ?", (event_id,))
        await services.mark_event(c, event_id, "pending", error="boom")
    async with db.reader() as c:
        after = await services.get_event(c, event_id)
        missing = await services.get_event(c, 999)
    assert before.target_id == -1 and before.attempts == 0
    assert after.target_id == -2 and after.attempts == 1 and after.last_error == "boom"
    assert after.payload == {"lang": "ru"}
    assert missing is None


async def test_farewell_payload_carries_chat_language(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    await _register(services, db, 1, -200)
    async with db.transaction() as c:
        await services.set_chat_lang(c, -100, "ru")
        await services.unregister_chat(c, -100)
        await services.unregister_chat(c, -200)
    async with db.reader() as c:
        events = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert {e.target_id: e.payload for e in events} == {-100: {"lang": "ru"}, -200: {"lang": "en"}}


async def test_unregister_without_farewell_queues_nothing(db: Database):
    services = Services()
    await _register(services, db, 1, -100)
    async with db.transaction() as c:
        await services.unregister_chat(c, -100, farewell=False)
    async with db.reader() as c:
        assert await services.due_events(c, "9999-01-01T00:00:00+00:00") == []


async def test_root_revoked_payload_carries_previous_root_language(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1, private_contact=True)
        await services.set_root(c, 1)
        await services.set_user_lang(c, 1, "ru")
        await services.set_root(c, 2)
    async with db.reader() as c:
        (event,) = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert event.event_type == "root_revoked"
    assert event.payload == {"lang": "ru"}


async def test_set_root_cancels_pending_root_revoked_for_new_root(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1, private_contact=True)
        await services.touch_user(c, 3, private_contact=True)
        await services.set_root(c, 1)
        await services.set_root(c, 2)  # 1 is revoked, notice pending
        await services.set_root(c, 3)  # 2 never wrote privately: no notice
    async with db.reader() as c:
        pending = await services.due_events(c, "9999-01-01T00:00:00+00:00")
    assert [(e.target_id, e.event_type) for e in pending] == [(1, "root_revoked")]

    async with db.transaction() as c:
        result = await services.set_root(c, 1)  # 1 is promoted again
    assert result.changed is True
    async with db.reader() as c:
        left = await services.due_events(c, "9999-01-01T00:00:00+00:00")
        assert [e.target_id for e in left] == [3]  # only the newly revoked root is notified
        cursor = await c.execute(
            "SELECT status FROM outbox WHERE event_type = 'root_revoked' AND target_id = 1"
        )
        assert [r[0] for r in await cursor.fetchall()] == ["cancelled"]


async def test_set_root_same_id_keeps_pending_notices(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1, private_contact=True)
        await services.set_root(c, 1)
        await services.set_root(c, 2)
        result = await services.set_root(c, 2)
    assert result.changed is False
    async with db.reader() as c:
        assert len(await services.due_events(c, "9999-01-01T00:00:00+00:00")) == 1


async def test_set_update_outcome_changes_a_claimed_update(db):
    services = Services()
    async with db.transaction() as c:
        assert await services.claim_update(c, 1, 42)
        await services.set_update_outcome(c, 1, 42, "ignored")
    async with db.reader() as c:
        cursor = await c.execute(
            "SELECT outcome FROM processed_updates WHERE bot_id = 1 AND update_id = 42"
        )
        assert (await cursor.fetchone())[0] == "ignored"


async def _reconcile_event(services, c, root_id: int) -> None:
    await services.queue_event(
        c, event_key=f"reconcile_removed:{root_id}:t", event_type="reconcile_removed",
        target_kind="user", target_id=root_id, payload={"lang": "en", "chat_ids": [-1]},
    )


async def test_set_root_retargets_pending_reconcile_notice_to_reachable_new_root(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1, private_contact=True)
        await services.set_root(c, 1)
        await _reconcile_event(services, c, 1)
        await services.touch_user(c, 2, private_contact=True)
        await services.set_user_lang(c, 2, "ru")
    async with db.transaction() as c:
        await services.set_root(c, 2)
    async with db.reader() as c:
        cur = await c.execute(
            "SELECT target_id, status, payload FROM outbox WHERE event_type = 'reconcile_removed'"
        )
        rows = await cur.fetchall()
    assert rows == [(2, "pending", '{"lang": "ru", "chat_ids": [-1]}')]


async def test_set_root_cancels_pending_reconcile_notice_when_new_root_is_unreachable(db: Database):
    services = Services()
    async with db.transaction() as c:
        await services.touch_user(c, 1, private_contact=True)
        await services.set_root(c, 1)
        await _reconcile_event(services, c, 1)
    async with db.transaction() as c:
        await services.set_root(c, 2)  # no private contact
    async with db.reader() as c:
        cur = await c.execute(
            "SELECT target_id, status FROM outbox WHERE event_type = 'reconcile_removed'"
        )
        assert await cur.fetchall() == [(1, "cancelled")]


async def test_prune_outbox_deletes_only_finished_rows_older_than_cutoff(db: Database):
    services = Services()
    async with db.transaction() as c:
        for n, status in enumerate(("sent", "failed", "cancelled", "pending")):
            await services.queue_event(
                c, event_key=f"k{n}", event_type="chat_farewell", target_kind="chat",
                target_id=-n - 1, generation=n, payload={},
            )
            await c.execute("UPDATE outbox SET status = ? WHERE event_key = ?", (status, f"k{n}"))
        await c.execute("UPDATE outbox SET updated_at = '2026-01-10T00:00:00+00:00' WHERE event_key = 'k0'")
        await c.execute("UPDATE outbox SET updated_at = '2026-01-01T00:00:00+00:00' WHERE event_key != 'k0'")
    async with db.transaction() as c:
        deleted = await services.prune_outbox(c, older_than="2026-01-05T00:00:00+00:00")
    assert deleted == 2  # failed and cancelled; k0 is newer, k3 is pending
    async with db.reader() as c:
        cur = await c.execute("SELECT event_key FROM outbox ORDER BY event_key")
        assert [r[0] for r in await cur.fetchall()] == ["k0", "k3"]
