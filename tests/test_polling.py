import asyncio
from datetime import datetime, timezone

import pytest
from aiogram import exceptions as aiogram_exceptions
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)

from app.delivery import AmbiguousSend, Delivery, PermanentSend, RateLimited
from app.handlers import Context
from app.services import Services
from app.telegram import AiogramTransport, to_event, run_polling

from conftest import FakeClock, RecordingTransport

BOT_ID = 999
BOT_USERNAME = "upb_bot"


def _mk_ctx(db, clock=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db, services, transport, clock=clock)
    ctx = Context(
        db=db,
        services=services,
        delivery=delivery,
        bot_id=BOT_ID,
        bot_username=BOT_USERNAME,
        clock=clock,
    )
    return ctx, services, transport, clock


def _bare_update(update_id: int) -> Update:
    # no message/my_chat_member/chat_member: to_event returns None for it, but the
    # polling loop must still advance its offset past this id
    return Update(update_id=update_id)


class _FakeBot:
    """Serves prepared batches of updates from get_updates, one per call."""

    def __init__(self, batches: list[list[Update]], stop: asyncio.Event | None = None):
        self._batches = list(batches)
        self._stop = stop
        self.offsets_requested: list[int | None] = []

    async def get_updates(self, offset=None, timeout=None, allowed_updates=None):
        self.offsets_requested.append(offset)
        if not self._batches:
            if self._stop is not None:
                self._stop.set()
            return []
        return self._batches.pop(0)


async def _run_until_stopped(ctx, bot: _FakeBot, stop: asyncio.Event, timeout: float = 2.0) -> None:
    await asyncio.wait_for(run_polling(ctx, bot, stop), timeout=timeout)


# --- offset bookkeeping ---


@pytest.mark.asyncio
async def test_offset_advances_only_after_batch_is_committed(db):
    ctx, services, transport, clock = _mk_ctx(db)
    stop = asyncio.Event()
    bot = _FakeBot([[_bare_update(5), _bare_update(6), _bare_update(7)]], stop=stop)

    await _run_until_stopped(ctx, bot, stop)

    async with db.reader() as c:
        offset = await services.get_offset(c, BOT_ID)
    assert offset == 8  # max update_id + 1, persisted only once the batch is done
    # the next get_updates call (or the terminating empty-batch call) requested it
    assert bot.offsets_requested[-1] in (8, 0)


@pytest.mark.asyncio
async def test_offset_not_advanced_if_a_handler_raises_mid_batch(db, monkeypatch):
    ctx, services, transport, clock = _mk_ctx(db)
    stop = asyncio.Event()
    bot = _FakeBot([[_bare_update(10), _bare_update(11)]], stop=stop)

    calls = []

    async def _boom(ctx_arg, event):
        calls.append(event.update_id)
        if len(calls) == 2:
            raise RuntimeError("simulated crash mid-batch")

    import app.telegram as telegram_module

    # only the second update in the batch is a real event (kind supported); force
    # both to be dispatched by making to_event return a minimal event for bare updates
    def _fake_to_event(update):
        from app.models import IncomingEvent

        return IncomingEvent(kind="message", update_id=update.update_id, chat_id=1, chat_type="private")

    monkeypatch.setattr(telegram_module, "to_event", _fake_to_event)
    monkeypatch.setattr(telegram_module, "handle_event", _boom)

    with pytest.raises(RuntimeError):
        await _run_until_stopped(ctx, bot, stop)

    async with db.reader() as c:
        offset = await services.get_offset(c, BOT_ID)
    assert offset == 0  # never persisted: the batch never fully committed


@pytest.mark.asyncio
async def test_lower_update_id_after_idle_gap_is_not_discarded(db):
    ctx, services, transport, clock = _mk_ctx(db)
    stop = asyncio.Event()
    # first batch advances the offset past 100; Telegram may later hand back an id
    # lower than that after a long idle gap (per the plan, this must still be processed)
    bot = _FakeBot(
        [
            [_bare_update(100)],
            [_bare_update(50)],
        ],
        stop=stop,
    )

    await _run_until_stopped(ctx, bot, stop)

    async with db.reader() as c:
        offset = await services.get_offset(c, BOT_ID)
    # both updates were processed and folded into a monotonically increasing
    # persisted offset; nothing about a "lower id" caused it to be skipped
    assert offset == 51


def _private_message_update(update_id: int, user_id: int = 42) -> Update:
    chat = Chat(id=user_id, type="private")
    msg = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=_user(user_id=user_id, first_name="U"),
        text="hello",
    )
    return Update(update_id=update_id, message=msg)


@pytest.mark.asyncio
async def test_replayed_batch_after_crash_is_safe_via_claim_update(db):
    ctx, services, transport, clock = _mk_ctx(db)

    async def _once(update_id):
        stop = asyncio.Event()
        bot = _FakeBot([[_private_message_update(update_id)]], stop=stop)
        await _run_until_stopped(ctx, bot, stop)

    await _once(5)
    async with db.transaction() as c:
        first_claim = await services.claim_update(c, BOT_ID, 5)
    assert first_claim is False  # already claimed by the (processed) update above

    # simulate a crash-then-replay: same update_id processed again
    await _once(5)
    async with db.reader() as c:
        offset = await services.get_offset(c, BOT_ID)
    assert offset == 6  # replay is safe: no error, offset lands in the same place
    assert transport.calls == []  # plain text in private: silence, both times


# --- exception translation (no live bot) ---


class _RaisingBot:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def send_message(self, chat_id, text, **kwargs):
        raise self._exc


@pytest.mark.asyncio
async def test_retry_after_maps_to_rate_limited():
    exc = aiogram_exceptions.TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=7)
    transport = AiogramTransport(_RaisingBot(exc))
    with pytest.raises(RateLimited) as info:
        await transport.send_message(1, "hi")
    assert info.value.retry_after == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        aiogram_exceptions.TelegramForbiddenError(method=None, message="bot was blocked by the user"),
        aiogram_exceptions.TelegramNotFound(method=None, message="chat not found"),
        aiogram_exceptions.TelegramBadRequest(method=None, message="message to reply not found"),
    ],
)
async def test_permanent_failures_map_to_permanent_send(exc):
    transport = AiogramTransport(_RaisingBot(exc))
    with pytest.raises(PermanentSend):
        await transport.send_message(1, "hi")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        aiogram_exceptions.TelegramServerError(method=None, message="internal server error"),
        aiogram_exceptions.TelegramNetworkError(method=None, message="connection reset"),
        aiogram_exceptions.TelegramConflictError(method=None, message="terminated by other getUpdates"),
        RuntimeError("unexpected"),
    ],
)
async def test_unclear_failures_map_to_ambiguous_send(exc):
    transport = AiogramTransport(_RaisingBot(exc))
    with pytest.raises(AmbiguousSend):
        await transport.send_message(1, "hi")


@pytest.mark.asyncio
async def test_no_secret_leaks_through_the_translated_exception():
    fake_token = "FAKE-TOKEN-MARKER-1a2b"
    exc = aiogram_exceptions.TelegramBadRequest(method=None, message=f"body contains {fake_token}")
    transport = AiogramTransport(_RaisingBot(exc))
    with pytest.raises(PermanentSend) as info:
        await transport.send_message(1, "hi")
    assert fake_token not in str(info.value)
    assert info.value.args == ()


# --- to_event mapping (real aiogram types, no network) ---


def _chat(chat_id=100, chat_type="group", title="G"):
    return Chat(id=chat_id, type=chat_type, title=title)


def _user(user_id=5, is_bot=False, first_name="Alice", username="alice"):
    return User(id=user_id, is_bot=is_bot, first_name=first_name, username=username)


def test_to_event_maps_a_command_message_with_entities():
    chat = _chat()
    msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=_user(),
        text="/upb list",
        entities=[MessageEntity(type="bot_command", offset=0, length=4)],
    )
    event = to_event(Update(update_id=10, message=msg))
    assert event is not None
    assert event.kind == "message"
    assert event.user_id == 5
    assert event.chat_type == "group"
    assert event.entities == (("bot_command", 0, 4),)
    assert event.text == "/upb list"
    assert event.edited is False


def test_to_event_channel_post_via_sender_chat_has_no_identified_user():
    chat = _chat(chat_type="supergroup")
    msg = Message(
        message_id=2,
        date=datetime.now(timezone.utc),
        chat=chat,
        sender_chat=_chat(chat_id=-1001, chat_type="channel", title="Chan"),
        from_user=_user(user_id=777, first_name="GroupAnonymousBot"),
        text="/upb list",
        entities=[MessageEntity(type="bot_command", offset=0, length=4)],
    )
    event = to_event(Update(update_id=11, message=msg))
    assert event is not None
    assert event.user_id is None


def test_to_event_maps_left_chat_member_service_message():
    chat = _chat()
    msg = Message(
        message_id=3,
        date=datetime.now(timezone.utc),
        chat=chat,
        left_chat_member=_user(user_id=42, first_name="Bob"),
    )
    event = to_event(Update(update_id=12, message=msg))
    assert event is not None
    assert event.kind == "member_left"
    assert event.left_user_id == 42


def test_to_event_maps_migration_service_message():
    chat = _chat()
    msg = Message(
        message_id=4,
        date=datetime.now(timezone.utc),
        chat=chat,
        migrate_to_chat_id=-1009999,
    )
    event = to_event(Update(update_id=13, message=msg))
    assert event is not None
    assert event.migrate_to_chat_id == -1009999


def test_to_event_maps_chat_member_leave():
    chat = _chat()
    cmu = ChatMemberUpdated(
        chat=chat,
        from_user=_user(),
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberMember(user=_user(user_id=8, first_name="C")),
        new_chat_member=ChatMemberLeft(user=_user(user_id=8, first_name="C")),
    )
    event = to_event(Update(update_id=14, chat_member=cmu))
    assert event is not None
    assert event.kind == "chat_member"
    assert event.left_user_id == 8
    assert event.bot_removed is False  # bot_removed only set for my_chat_member


def test_to_event_maps_my_chat_member_bot_removed():
    chat = _chat()
    cmu = ChatMemberUpdated(
        chat=chat,
        from_user=_user(),
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberMember(user=_user(user_id=BOT_ID, first_name="Bot")),
        new_chat_member=ChatMemberLeft(user=_user(user_id=BOT_ID, first_name="Bot")),
    )
    event = to_event(Update(update_id=15, my_chat_member=cmu))
    assert event is not None
    assert event.kind == "my_chat_member"
    assert event.bot_removed is True


def test_to_event_returns_none_for_unsupported_update():
    assert to_event(Update(update_id=16)) is None
