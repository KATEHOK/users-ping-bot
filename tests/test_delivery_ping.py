import re

import pytest

from app.delivery import AmbiguousSend, Delivery, PermanentSend, RateLimited
from app.models import SubscriberRef
from app.rendering import PONG
from app.services import Services

from conftest import FakeClock, RecordingTransport

CHAT = 100
MSG = 7
THREAD = 3


def _mk_delivery(db, clock=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    return Delivery(db, services, transport, clock=clock), services, transport, clock


async def _register_and_subscribe(db, services, chat_id, registrar_id, user_ids):
    async with db.transaction() as c:
        await services.touch_user(c, registrar_id)
        await services.set_root(c, registrar_id)  # staff grounds for run_ping's initiator checks
        result = await services.register_chat(c, chat_id, "Chat", registrar_id)
        for uid in user_ids:
            await services.touch_user(c, uid, display_name=f"User{uid}")
            await services.subscribe(c, chat_id, uid)
    return result.generation


def _ids_in(text: str) -> set[int]:
    return {int(m) for m in re.findall(r'tg://user\?id=(\d+)"', text)}


@pytest.mark.asyncio
async def test_empty_snapshot_replies_pong(db):
    delivery, services, transport, clock = _mk_delivery(db)
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        result = await services.register_chat(c, CHAT, "Chat", 1)

    await delivery.run_ping(CHAT, 1, MSG, THREAD, result.generation, [])

    assert len(transport.calls) == 1
    assert transport.calls[0]["text"] == PONG
    assert transport.calls[0]["reply_to_message_id"] == MSG
    assert transport.calls[0]["thread_id"] == THREAD


@pytest.mark.asyncio
async def test_long_list_splits_without_loss_or_duplication(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = list(range(1000, 1000 + 25))
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)

    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    # force several mentions per chunk but still several chunks
    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=200)

    assert len(transport.calls) > 1, "expected more than one chunk"
    seen: list[int] = []
    for call in transport.calls:
        assert call["reply_to_message_id"] == MSG
        assert call["thread_id"] == THREAD
        seen.extend(_ids_in(call["text"]))
    assert sorted(seen) == sorted(user_ids)
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_ping_over_60_seconds_completes_in_full(db):
    clock = FakeClock()
    delivery, services, transport, clock = _mk_delivery(db, clock)
    user_ids = [2001, 2002, 2003, 2004, 2005]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)

    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    # one mention per chunk; force a 429 with a long retry_after before two chunks
    transport.queue_raises(
        [
            RateLimited(retry_after=25.0),
            RateLimited(retry_after=25.0),
            RateLimited(retry_after=25.0),
        ]
    )
    start = clock.now()
    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)
    elapsed = (clock.now() - start).total_seconds()

    assert elapsed > 60
    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert sorted(set(seen)) == sorted(user_ids)


@pytest.mark.asyncio
async def test_unsubscribe_between_chunks_removes_only_that_recipient(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [3001, 3002, 3003]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)

    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    # unsubscribe 3002 right after the first chunk is recorded, before the next
    # chunk's re-check by monkeypatching services.subscription_id_of ordering:
    # simplest is to unsubscribe between awaits using a one-shot hook on the
    # transport call count.
    original_send = transport.send_message

    async def send_and_maybe_unsubscribe(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.unsubscribe(c, CHAT, 3002)

    transport.send_message = send_and_maybe_unsubscribe

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert 3002 not in seen
    assert set(seen) == {3001, 3003}
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_new_subscriber_is_not_added_mid_ping(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [4001, 4002]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)

    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_add_subscriber(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.touch_user(c, 4099)
                await services.subscribe(c, CHAT, 4099)

    transport.send_message = send_and_add_subscriber

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert 4099 not in seen
    assert set(seen) == {4001, 4002}


@pytest.mark.asyncio
async def test_off_then_on_does_not_restore_old_subscription_into_ping(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [5001, 5002]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)

    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_cycle(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.unsubscribe(c, CHAT, 5002)
                await services.subscribe(c, CHAT, 5002)  # new subscription_id

    transport.send_message = send_and_cycle

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    # 5001's chunk went out first; by the time 5002's chunk is re-filtered, the
    # off->on cycle has already minted a new subscription_id, so 5002 (old id)
    # is dropped rather than resent
    assert 5001 in seen
    assert 5002 not in seen


@pytest.mark.asyncio
async def test_initiator_losing_all_grounds_cancels_remainder(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [6001, 6002, 6003]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    # initiator is 6001, a plain subscriber (no staff role)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_unsub_initiator(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.unsubscribe(c, CHAT, 6001)

    transport.send_message = send_and_unsub_initiator

    await delivery.run_ping(CHAT, 6001, MSG, THREAD, generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert len(seen) < len(user_ids)  # remainder cancelled


@pytest.mark.asyncio
async def test_admin_who_unsubscribed_self_may_continue(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [7001, 7002, 7003]
    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.touch_user(c, 7001)
        await services.grant_admin(c, 7001)
        result = await services.register_chat(c, CHAT, "Chat", 1)
        for uid in user_ids:
            await services.touch_user(c, uid, display_name=f"User{uid}")
            await services.subscribe(c, CHAT, uid)

    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_unsub_admin(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.unsubscribe(c, CHAT, 7001)

    transport.send_message = send_and_unsub_admin

    await delivery.run_ping(CHAT, 7001, MSG, THREAD, result.generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    # admin's own subscription drops out (rule 2), but the remainder is not
    # cancelled: the other two subscribers still get pinged (rule 3)
    assert {7002, 7003} <= set(seen)


@pytest.mark.asyncio
async def test_unregistration_cancels_remainder(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [8001, 8002]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_unregister(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.unregister_chat(c, CHAT)

    transport.send_message = send_and_unregister

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert len(seen) < len(user_ids)


@pytest.mark.asyncio
async def test_generation_change_cancels_remainder(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [8101, 8102]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_reregister(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.unregister_chat(c, CHAT)
                await services.register_chat(c, CHAT, "Chat", 1)

    transport.send_message = send_and_reregister

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert len(seen) < len(user_ids)


@pytest.mark.asyncio
async def test_open_conflict_cancels_remainder(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [8201, 8202]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_open_conflict(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.open_conflict(c, [CHAT], "test_conflict")

    transport.send_message = send_and_open_conflict

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert len(seen) < len(user_ids)


@pytest.mark.asyncio
async def test_emptied_mid_way_is_silent_no_trailing_pong(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [9001, 9002]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    original_send = transport.send_message

    async def send_and_unsub_all(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.unsubscribe(c, CHAT, 9002)

    transport.send_message = send_and_unsub_all

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    for call in transport.calls:
        assert call["text"] != PONG


@pytest.mark.asyncio
async def test_ambiguous_error_stops_remainder_no_retry(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [10001, 10002, 10003]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    transport.raise_next(AmbiguousSend())

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    # the disputed first chunk's attempt was made (and recorded), but nothing
    # after it: no automatic retry, remainder stopped
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_429_retries_up_to_three_then_gives_up(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [11001]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    transport.queue_raises([RateLimited(1.0)] * 4)  # one more than allowed

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    # 3 retries + the original attempt = 4 calls, then give up
    assert len(transport.calls) == 4


@pytest.mark.asyncio
async def test_429_retry_honours_retry_after_and_successful_chunk_resets_counter(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [12001, 12002]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    # first chunk (user 12001): 2 429s then success; second chunk (12002): 2
    # more 429s then success. 2+2 would exceed MAX_PING_RETRIES=3 if the
    # counter carried over between chunks, so this only passes if it reset.
    transport.queue_raises([RateLimited(5.0), RateLimited(5.0)])
    original_send = transport.send_message

    async def send_and_requeue(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 3:  # chunk 1 just succeeded on its 3rd attempt
            transport.queue_raises([RateLimited(5.0), RateLimited(5.0)])

    transport.send_message = send_and_requeue

    start = clock.now()
    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)
    elapsed = (clock.now() - start).total_seconds()

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert sorted(set(seen)) == sorted(user_ids)
    assert elapsed >= 20  # four waits of 5s honoured


@pytest.mark.asyncio
async def test_reply_impossible_drops_ping_no_detached_send(db):
    delivery, services, transport, clock = _mk_delivery(db)
    user_ids = [13001]
    generation = await _register_and_subscribe(db, services, CHAT, 1, user_ids)
    async with db.reader() as c:
        subs = await services.list_subscribers(c, CHAT)
    snapshot = [SubscriberRef(s.user_id, s.subscription_id, s.display_name, s.username) for s in subs]

    transport.raise_next(PermanentSend("message to reply to was deleted"))

    await delivery.run_ping(CHAT, 1, MSG, THREAD, generation, snapshot, chunk_limit=10)

    assert len(transport.calls) == 1
    # every attempt made was still addressed as a reply; nothing sent detached
    for call in transport.calls:
        assert call["reply_to_message_id"] == MSG
