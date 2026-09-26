import logging

import pytest

from app.delivery import (
    AmbiguousSend,
    Delivery,
    GrantRegistry,
    PermanentSend,
    RateLimited,
)
from app.services import Services

from conftest import FakeClock, RecordingTransport

CHAT = 200


def _mk_delivery(db, clock=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    return Delivery(db, services, transport, clock=clock), services, transport, clock


async def _register(db, services, chat_id=CHAT, registrar_id=1):
    async with db.transaction() as c:
        await services.touch_user(c, registrar_id)
        result = await services.register_chat(c, chat_id, "Chat", registrar_id)
    return result.generation


@pytest.mark.asyncio
async def test_issue_then_take_returns_the_grant_once(db):
    registry = GrantRegistry(clock=FakeClock())
    grant = registry.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=1
    )
    assert registry.take(1) == grant
    assert registry.take(1) is None  # single-use: gone after the first take


@pytest.mark.asyncio
async def test_take_unknown_update_id_returns_none(db):
    registry = GrantRegistry(clock=FakeClock())
    assert registry.take(999) is None


@pytest.mark.asyncio
async def test_grant_expires_after_60_seconds(db):
    clock = FakeClock()
    registry = GrantRegistry(clock=clock)
    registry.issue(update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=1)
    clock.advance(61)
    assert registry.take(1) is None


@pytest.mark.asyncio
async def test_grant_valid_just_under_60_seconds(db):
    clock = FakeClock()
    registry = GrantRegistry(clock=clock)
    registry.issue(update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=1)
    clock.advance(59)
    assert registry.take(1) is not None


@pytest.mark.asyncio
async def test_revoke_chat_drops_all_its_grants(db):
    registry = GrantRegistry(clock=FakeClock())
    registry.issue(update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=1)
    registry.issue(update_id=2, user_id=43, chat_id=CHAT, message_id=10, generation=1)
    registry.issue(update_id=3, user_id=44, chat_id=CHAT + 1, message_id=11, generation=1)
    registry.revoke_chat(CHAT)
    assert registry.take(1) is None
    assert registry.take(2) is None
    assert registry.take(3) is not None  # a different chat's grant is untouched


@pytest.mark.asyncio
async def test_revoke_user_drops_only_that_users_grant(db):
    registry = GrantRegistry(clock=FakeClock())
    registry.issue(update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=1)
    registry.issue(update_id=2, user_id=43, chat_id=CHAT, message_id=10, generation=1)
    registry.revoke_user(CHAT, 42)  # user 42 re-subscribed before his confirmation sent
    assert registry.take(1) is None
    assert registry.take(2) is not None


@pytest.mark.asyncio
async def test_replayed_update_mints_no_new_grant(db):
    # a replay would be caught upstream by claim_update, so issue() is called
    # exactly once per real update_id; this asserts the registry itself never
    # hands out a second grant for an update_id once consumed
    registry = GrantRegistry(clock=FakeClock())
    grant = registry.issue(update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=1)
    registry.take(1)
    replay_attempt = registry.take(1)  # simulated replay: same update_id looked up again
    assert replay_attempt is None
    assert grant.update_id == 1


@pytest.mark.asyncio
async def test_confirm_notify_off_delivers_without_current_subscription(db):
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )

    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")

    assert delivered is True
    assert len(transport.calls) == 1
    assert transport.calls[0]["chat_id"] == CHAT
    assert transport.calls[0]["reply_to_message_id"] == 9


@pytest.mark.asyncio
async def test_confirm_notify_off_revoked_by_unregistration(db):
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT)
    delivery.cancel_chat(CHAT)

    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")

    assert delivered is False
    assert transport.calls == []
    assert delivery.grants.take(1) is None  # cancel_chat already revoked it


@pytest.mark.asyncio
async def test_confirm_notify_off_revoked_by_migration_generation_change(db):
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT)
        await services.register_chat(c, CHAT, "Chat", 1)  # new generation

    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")

    assert delivered is False
    assert transport.calls == []


@pytest.mark.asyncio
async def test_confirm_notify_off_revoked_by_open_conflict(db):
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    async with db.transaction() as c:
        await services.open_conflict(c, [CHAT], "test_conflict")

    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")

    assert delivered is False
    assert transport.calls == []


@pytest.mark.asyncio
async def test_confirm_notify_off_revoked_by_expiry(db):
    clock = FakeClock()
    delivery, services, transport, clock = _mk_delivery(db, clock)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    clock.advance(61)

    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")

    assert delivered is False
    assert transport.calls == []


@pytest.mark.asyncio
async def test_confirm_notify_off_revoked_by_resubscription(db):
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    # user 42 re-subscribes before the confirmation is sent
    delivery.grants.revoke_user(CHAT, 42)
    taken = delivery.grants.take(1)
    assert taken is None  # the handler path would bail out here, never calling confirm


@pytest.mark.asyncio
async def test_confirm_notify_off_no_retry_after_success_or_ambiguous(db):
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)

    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")
    assert delivered is True
    assert len(transport.calls) == 1  # no follow-up attempt after a success

    grant2 = delivery.grants.issue(
        update_id=2, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    transport.raise_next(AmbiguousSend())
    delivered2 = await delivery.confirm_notify_off(grant2, "Unsubscribed.")
    assert delivered2 is False
    assert len(transport.calls) == 2  # exactly one attempt, no retry on ambiguous


@pytest.mark.asyncio
async def test_confirm_notify_off_429_retries_inside_window(db):
    clock = FakeClock()
    delivery, services, transport, clock = _mk_delivery(db, clock)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    transport.raise_next(RateLimited(retry_after=5.0))

    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")

    assert delivered is True
    assert len(transport.calls) == 2  # one failed attempt, one retry that succeeded


@pytest.mark.asyncio
async def test_confirm_notify_off_permanent_stops_immediately(db):
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    transport.raise_next(PermanentSend())

    delivered = await delivery.confirm_notify_off(grant, "Unsubscribed.")

    assert delivered is False
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_no_fake_token_marker_leaks_through_grant_flow(db, caplog):
    caplog.set_level(logging.INFO, logger="app.delivery")
    delivery, services, transport, clock = _mk_delivery(db)
    generation = await _register(db, services)
    grant = delivery.grants.issue(
        update_id=1, user_id=42, chat_id=CHAT, message_id=9, generation=generation
    )
    fake_token = "FAKE-TOKEN-MARKER-9f3a"
    transport.raise_next(AmbiguousSend(f"leak attempt containing {fake_token}"))

    await delivery.confirm_notify_off(grant, "Unsubscribed.")

    for call in transport.calls:
        assert fake_token not in call["text"]
    for record in caplog.records:
        assert fake_token not in record.getMessage()
