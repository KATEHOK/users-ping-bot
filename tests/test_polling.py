"""aiogram adapter and polling loop, startup reconciliation, exit policy."""

import asyncio
import json
import logging
import signal
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import exceptions as aex
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberRestricted,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)

import app.__main__ as entry
from app import rendering
from app.config import Config
from app.db import open_database
from app.delivery import (
    AmbiguousSend,
    ChatMigrated,
    Delivery,
    PermanentSend,
    RateLimited,
    Unauthorized,
)
from app.handlers import Context
from app.services import Services
from app.telegram import AiogramTransport, PollingFailed, map_error, run_polling, to_event

from conftest import (
    BOT_ID,
    FakeClock,
    RecordingTransport,
    make_admin,
    make_root,
    mk_ctx,
    register_chat,
    subscribe,
)

MARKER = "FAKE-TOKEN-MARKER-1a2b3c"
NOW = datetime.now(timezone.utc)


def _user(user_id=5, is_bot=False, first_name="Alice", username="alice"):
    return User(id=user_id, is_bot=is_bot, first_name=first_name, username=username)


def _chat(chat_id=100, chat_type="group", title="G"):
    return Chat(id=chat_id, type=chat_type, title=title)


def _cmd_update(update_id, text="/upb notify on", *, user_id=5, chat_id=500, **msg_kw):
    msg = Message(
        message_id=update_id,
        date=NOW,
        chat=_chat(chat_id),
        from_user=_user(user_id),
        text=text,
        entities=[MessageEntity(type="bot_command", offset=0, length=4)],
        **msg_kw,
    )
    return Update(update_id=update_id, message=msg)


def _bare(update_id):
    return Update(update_id=update_id)


# --- to_event ---


def test_to_event_maps_a_command_message_with_entities():
    event = to_event(_cmd_update(10, "/upb list"))
    assert event is not None
    assert (event.kind, event.user_id, event.chat_type) == ("message", 5, "group")
    assert event.entities == (("bot_command", 0, 4),)
    assert event.text == "/upb list" and event.edited is False


def test_thread_id_is_set_only_for_topic_messages():
    topic = to_event(_cmd_update(1, message_thread_id=77, is_topic_message=True))
    reply_thread = to_event(_cmd_update(2, message_thread_id=78))  # reply chain in a plain group
    plain = to_event(_cmd_update(3))
    assert (topic.thread_id, reply_thread.thread_id, plain.thread_id) == (77, None, None)


def test_to_event_channel_post_via_sender_chat_has_no_identified_user():
    msg = Message(
        message_id=2, date=NOW, chat=_chat(chat_type="supergroup"),
        sender_chat=_chat(chat_id=-1001, chat_type="channel", title="Chan"),
        from_user=_user(user_id=777), text="/upb list",
    )
    assert to_event(Update(update_id=11, message=msg)).user_id is None


def test_to_event_maps_left_chat_member_and_migration():
    left = Message(message_id=3, date=NOW, chat=_chat(), left_chat_member=_user(user_id=42))
    event = to_event(Update(update_id=12, message=left))
    assert (event.kind, event.left_user_id) == ("member_left", 42)
    mig = Message(message_id=4, date=NOW, chat=_chat(), migrate_to_chat_id=-1009999)
    assert to_event(Update(update_id=13, message=mig)).migrate_to_chat_id == -1009999


def _cmu(new_cls, uid):
    return ChatMemberUpdated(
        chat=_chat(), from_user=_user(), date=NOW,
        old_chat_member=ChatMemberMember(user=_user(user_id=uid)),
        new_chat_member=new_cls(user=_user(user_id=uid)),
    )


def test_to_event_maps_chat_member_leave_and_bot_removal():
    ev = to_event(Update(update_id=14, chat_member=_cmu(ChatMemberLeft, 8)))
    assert (ev.kind, ev.left_user_id, ev.bot_removed) == ("chat_member", 8, False)
    ev = to_event(Update(update_id=15, my_chat_member=_cmu(ChatMemberLeft, BOT_ID)))
    assert (ev.kind, ev.bot_removed) == ("my_chat_member", True)
    ev = to_event(Update(update_id=16, my_chat_member=_cmu(ChatMemberMember, BOT_ID)))
    assert ev.bot_removed is False


def _restricted(uid, is_member):
    rights = {n: False for n in ChatMemberRestricted.model_fields if n.startswith("can_")}
    return ChatMemberRestricted(
        user=_user(user_id=uid), is_member=is_member, until_date=0, **rights
    )


def test_a_restricted_non_member_bot_counts_as_removed_but_a_member_does_not():
    def cmu(new):
        return ChatMemberUpdated(
            chat=_chat(), from_user=_user(), date=NOW,
            old_chat_member=ChatMemberMember(user=_user(user_id=BOT_ID)), new_chat_member=new,
        )

    gone = to_event(Update(update_id=1, my_chat_member=cmu(_restricted(BOT_ID, False))))
    assert (gone.bot_removed, gone.left_user_id) == (True, BOT_ID)
    kept = to_event(Update(update_id=2, my_chat_member=cmu(_restricted(BOT_ID, True))))
    assert (kept.bot_removed, kept.left_user_id) == (False, None)
    member = to_event(Update(update_id=3, chat_member=cmu(_restricted(8, False))))
    assert (member.kind, member.left_user_id, member.bot_removed) == ("chat_member", 8, False)


def test_to_event_returns_none_for_unsupported_update():
    assert to_event(_bare(17)) is None


# --- error mapping ---


@pytest.mark.parametrize(
    "exc,expected",
    [
        (aex.TelegramForbiddenError(method=None, message="blocked"), PermanentSend),
        (aex.TelegramNotFound(method=None, message="chat not found"), PermanentSend),
        (aex.TelegramBadRequest(method=None, message="message to be replied not found"), PermanentSend),
        (aex.TelegramServerError(method=None, message="oops"), AmbiguousSend),
        (aex.TelegramNetworkError(method=None, message="reset"), AmbiguousSend),
        (aex.TelegramConflictError(method=None, message="conflict"), AmbiguousSend),
        (RuntimeError("unexpected"), AmbiguousSend),
        (asyncio.TimeoutError(), AmbiguousSend),
    ],
)
def test_error_mapping(exc, expected):
    assert type(map_error(exc)) is expected


def test_retry_after_migrate_and_unauthorized_mapping():
    rl = map_error(aex.TelegramRetryAfter(method=None, message="slow", retry_after=7))
    assert isinstance(rl, RateLimited) and rl.retry_after == 7
    mg = map_error(aex.TelegramMigrateToChat(method=None, message="moved", migrate_to_chat_id=-1005))
    assert isinstance(mg, ChatMigrated) and mg.new_chat_id == -1005
    assert isinstance(map_error(aex.TelegramUnauthorizedError(method=None, message="bad token")), Unauthorized)


def test_mapped_errors_carry_no_text_from_the_source():
    for exc in (
        aex.TelegramBadRequest(method=None, message=f"body {MARKER}"),
        aex.TelegramNetworkError(method=None, message=f"https://api.telegram.org/bot{MARKER}/x"),
        RuntimeError(MARKER),
    ):
        mapped = map_error(exc)
        assert MARKER not in str(mapped) and MARKER not in repr(mapped)


class _RaisingBot:
    id = BOT_ID

    def __init__(self, exc=None, status=None, is_member=None):
        self._exc, self._status, self._is_member = exc, status, is_member

    async def send_message(self, *a, **k):
        raise self._exc

    async def get_chat_member(self, chat_id, user_id):
        if self._exc is not None:
            raise self._exc
        attrs = {"status": self._status}
        if self._is_member is not None:
            attrs["is_member"] = self._is_member
        return type("M", (), attrs)()


async def test_transport_send_translates_and_hides_the_cause():
    transport = AiogramTransport(_RaisingBot(aex.TelegramBadRequest(method=None, message=MARKER)))
    with pytest.raises(PermanentSend) as info:
        await transport.send_message(1, "hi")
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize(
    "bot,expected",
    [
        (_RaisingBot(status="member"), None),
        (_RaisingBot(status="administrator"), None),
        (_RaisingBot(status="kicked"), PermanentSend),
        (_RaisingBot(status="left"), PermanentSend),
        (_RaisingBot(status="restricted", is_member=False), PermanentSend),
        (_RaisingBot(status="restricted", is_member=True), None),
        (_RaisingBot(aex.TelegramForbiddenError(method=None, message="x")), PermanentSend),
        (_RaisingBot(aex.TelegramNotFound(method=None, message="chat not found")), PermanentSend),
        (_RaisingBot(aex.TelegramMigrateToChat(method=None, message="m", migrate_to_chat_id=-9)), ChatMigrated),
        (_RaisingBot(aex.TelegramNetworkError(method=None, message="x")), AmbiguousSend),
    ],
)
async def test_probe_chat(bot, expected):
    transport = AiogramTransport(bot)
    if expected is None:
        await transport.probe_chat(1)
    else:
        with pytest.raises(expected):
            await transport.probe_chat(1)


# --- polling loop ---


class FakeBot:
    """Serves scripted get_updates results: a list of updates or an exception per call."""

    def __init__(self, script, stop=None):
        self.script = list(script)
        self.stop = stop
        self.offsets: list[int | None] = []

    async def get_updates(self, offset=None, timeout=None, allowed_updates=None):
        self.offsets.append(offset)
        if not self.script:
            if self.stop is not None:
                self.stop.set()
            return []
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


async def _poll(ctx, bot, stop, timeout=3.0):
    await asyncio.wait_for(run_polling(ctx, bot, stop), timeout)


async def test_first_call_has_no_offset_and_the_offset_is_kept_in_memory(db):
    ctx, services, transport, _c = mk_ctx(db)
    stop = asyncio.Event()
    bot = FakeBot([[_bare(5), _bare(6)], [_bare(7)]], stop)
    await _poll(ctx, bot, stop)
    assert bot.offsets == [None, 7, 8]
    assert not hasattr(services, "get_offset") and not hasattr(services, "set_offset")
    async with db.reader() as c:
        cur = await c.execute("SELECT name FROM sqlite_master WHERE name = 'polling_state'")
        assert await cur.fetchone() is None


async def test_restart_starts_without_offset_and_replayed_updates_are_deduplicated(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, 5)
    for run in (1, 2):
        stop = asyncio.Event()
        bot = FakeBot([[_cmd_update(1, "/upb chat register", user_id=5)]], stop)
        await _poll(ctx, bot, stop)
        assert bot.offsets[0] is None
    assert len(transport.calls) == 1  # the replay after the "restart" was a no-op


async def test_a_lower_update_id_after_an_idle_gap_is_still_processed(db):
    ctx, services, transport, _c = mk_ctx(db)
    stop = asyncio.Event()
    bot = FakeBot([[_cmd_update(100)], [_cmd_update(50)]], stop)
    await _poll(ctx, bot, stop)
    async with db.reader() as c:
        cur = await c.execute("SELECT COUNT(*) FROM processed_updates")
        assert (await cur.fetchone())[0] == 2


async def test_offset_advances_past_a_poison_update_and_it_is_recorded_as_error(db, monkeypatch):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, 5)

    real = services.register_chat
    async def boom(*a, **k):
        raise RuntimeError(MARKER)
    monkeypatch.setattr(services, "register_chat", boom)

    stop = asyncio.Event()
    updates = [_cmd_update(1, "/upb chat register"), _cmd_update(2, "/upb list"), _cmd_update(3, "/upb chat register")]
    bot = FakeBot([updates], stop)
    await _poll(ctx, bot, stop)
    monkeypatch.setattr(services, "register_chat", real)
    assert bot.offsets == [None, 4]
    async with db.reader() as c:
        cur = await c.execute("SELECT update_id, outcome FROM processed_updates ORDER BY update_id")
        assert await cur.fetchall() == [(1, "error"), (2, "ignored"), (3, "error")]


async def test_no_send_error_on_any_reply_path_crashes_polling(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, 5)
    await register_chat(db, services, 500, 5)
    await subscribe(db, services, 500, 21)
    errors = [PermanentSend(), AmbiguousSend(), RateLimited(0.0), ChatMigrated(-1000)]
    transport.queue_raises(errors * 4)
    stop = asyncio.Event()
    updates = [
        _cmd_update(1, "/upb notify on"),
        _cmd_update(2, "/upb all"),
        _cmd_update(3, "/upb list"),
        _cmd_update(4, "/upb help"),
        _cmd_update(5, "/upb chat register"),
        _cmd_update(6, "/upb lang ru"),
        _cmd_update(7, "/upb notify off"),
    ]
    bot = FakeBot([updates], stop)
    await _poll(ctx, bot, stop)  # completes normally
    async with db.reader() as c:
        cur = await c.execute("SELECT COUNT(*) FROM processed_updates")
        assert (await cur.fetchone())[0] == 7


async def test_unauthorized_from_a_reply_is_fatal_for_the_loop(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, 5)
    transport.queue_raises([Unauthorized()])
    stop = asyncio.Event()
    bot = FakeBot([[_cmd_update(1, "/upb chat register")]], stop)
    with pytest.raises(Unauthorized):
        await _poll(ctx, bot, stop)


async def test_ten_consecutive_failures_are_fatal_and_success_resets_the_count(db):
    ctx, services, transport, clock = mk_ctx(db)
    stop = asyncio.Event()
    boom = lambda: aex.TelegramNetworkError(method=None, message="x")  # noqa: E731
    bot = FakeBot([boom() for _ in range(9)] + [[]] + [boom() for _ in range(9)], stop)
    await _poll(ctx, bot, stop)  # 9 + reset + 9: never fatal
    bot = FakeBot([boom() for _ in range(10)])
    start = clock.now()
    with pytest.raises(PollingFailed):
        await _poll(ctx, bot, asyncio.Event())
    assert len(bot.offsets) == 10
    assert (clock.now() - start).total_seconds() == 45  # 9 backoffs of 5 s, none after the last


async def test_unauthorized_from_get_updates_is_fatal_at_once(db):
    ctx, *_ = mk_ctx(db)
    bot = FakeBot([aex.TelegramUnauthorizedError(method=None, message="Unauthorized")])
    with pytest.raises(Unauthorized):
        await _poll(ctx, bot, asyncio.Event())
    assert len(bot.offsets) == 1


async def test_network_errors_never_put_the_token_into_the_logs(db, caplog):
    caplog.set_level(logging.DEBUG)
    ctx, *_ = mk_ctx(db)
    url = f"https://api.telegram.org/bot{MARKER}/getUpdates"
    bot = FakeBot([RuntimeError(url), aex.TelegramNetworkError(method=None, message=url)] * 5)
    with pytest.raises(PollingFailed):
        await _poll(ctx, bot, asyncio.Event())
    assert "poll_error" in caplog.text
    assert MARKER not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)

    transport = AiogramTransport(_RaisingBot(aex.TelegramNetworkError(method=None, message=url)))
    delivery = Delivery(db, Services(), transport)
    await delivery.send_reply(1, "x", reply_to=None, thread_id=None)
    assert MARKER not in caplog.text


class BlockingClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds):
        self.sleeping.set()
        await asyncio.Event().wait()


async def test_error_backoff_is_interrupted_by_stop(db):
    clock = BlockingClock()
    ctx, *_ = mk_ctx(db, clock)
    stop = asyncio.Event()
    bot = FakeBot([RuntimeError("x")] * 3)
    task = asyncio.create_task(run_polling(ctx, bot, stop))
    await asyncio.wait_for(clock.sleeping.wait(), 2)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(bot.offsets) == 1


async def test_stop_interrupts_a_pending_long_poll(db):
    ctx, *_ = mk_ctx(db)
    stop = asyncio.Event()

    class Hanging:
        async def get_updates(self, **kw):
            await asyncio.Event().wait()

    task = asyncio.create_task(run_polling(ctx, Hanging(), stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_stop_mid_batch_leaves_the_rest_unhandled_and_a_restart_finishes_it(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, 5)
    stop = asyncio.Event()
    send = transport.send_message

    async def send_then_stop(*a, **k):
        stop.set()  # SIGTERM arrives while the first update is being answered
        await send(*a, **k)

    transport.send_message = send_then_stop
    batch = [_cmd_update(1, "/upb chat register"), _cmd_update(2, "/upb notify on"), _cmd_update(3, "/upb list")]
    bot = FakeBot([batch])
    await _poll(ctx, bot, stop)
    assert bot.offsets == [None]
    async with db.reader() as c:
        cur = await c.execute("SELECT update_id FROM processed_updates ORDER BY update_id")
        assert await cur.fetchall() == [(1,)]  # 2 and 3 stay unclaimed: Telegram redelivers them

    transport.send_message = send
    stop2 = asyncio.Event()
    await _poll(ctx, FakeBot([batch], stop2), stop2)  # after the restart
    async with db.reader() as c:
        cur = await c.execute("SELECT update_id FROM processed_updates ORDER BY update_id")
        assert await cur.fetchall() == [(1,), (2,), (3,)]
    assert len(transport.calls) == 3  # update 1 was not answered twice


# --- background loops survive transient errors ---


async def test_prune_loop_survives_a_transient_error(db, monkeypatch, caplog):
    import sqlite3

    ctx, *_ = mk_ctx(db)
    stop = asyncio.Event()
    calls = []

    async def flaky(_ctx):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        if len(calls) == 3:
            stop.set()
        return 0

    monkeypatch.setattr(entry, "prune_once", flaky)
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(entry.prune_loop(ctx, stop), 2)
    assert len(calls) == 3
    assert "prune_error exc=OperationalError" in caplog.text


async def test_outbox_loop_survives_a_transient_error_but_not_unauthorized(db, monkeypatch, caplog):
    import sqlite3

    ctx, services, transport, _c = mk_ctx(db)
    stop = asyncio.Event()
    real = services.due_events
    calls = []

    async def flaky(c, now_iso):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        if len(calls) == 3:
            stop.set()
        return await real(c, now_iso)

    monkeypatch.setattr(services, "due_events", flaky)
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(ctx.delivery.outbox_loop(stop), 2)
    assert len(calls) == 3
    assert "outbox_loop_error exc=OperationalError" in caplog.text

    async def revoked(c, now_iso):
        raise Unauthorized()

    monkeypatch.setattr(services, "due_events", revoked)
    with pytest.raises(Unauthorized):
        await asyncio.wait_for(ctx.delivery.outbox_loop(asyncio.Event()), 2)


# --- startup reconciliation and root report ---


async def _reg_world(db, services):
    await make_root(db, services, 10, contact=True)
    await make_admin(db, services, 1)
    await register_chat(db, services, -1, 1, "Gone")
    await register_chat(db, services, -2, 1, "Moved")
    await register_chat(db, services, -3, 1, "Flaky")
    await register_chat(db, services, -4, 1, "Fine")
    for chat in (-1, -2, -3, -4):
        await subscribe(db, services, chat, 20)


async def test_reconcile_unregisters_migrates_skips_and_keeps(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _reg_world(db, services)
    transport.set_probe(-1, PermanentSend())
    transport.set_probe(-2, ChatMigrated(-200))
    transport.set_probe(-3, AmbiguousSend())
    removed = await entry.reconcile_chats(ctx, transport)
    assert removed == [-1]
    async with db.reader() as c:
        ids = sorted(r.chat_id for r in await services.list_chats(c))
        assert ids == sorted([-200, -3, -4])
        assert await services.is_subscribed(c, -200, 20)
        assert await services.resolve_chat_id(c, -2) == -200
    assert sorted(transport.probes) == [-4, -3, -2, -1]
    assert transport.calls == []  # no farewell to a chat the bot is gone from
    async with db.reader() as c:
        cur = await c.execute("SELECT COUNT(*) FROM outbox")
        assert (await cur.fetchone())[0] == 0


async def test_report_goes_to_root_every_start_in_root_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _reg_world(db, services)
    await entry.send_startup_report(ctx, [-9, -8])
    await entry.send_startup_report(ctx, [])
    assert [c["chat_id"] for c in transport.calls] == [10, 10]
    first, second = (c["text"] for c in transport.calls)
    assert first.splitlines()[0] == "Bot started. Chats removed on check: 2."
    assert first.splitlines()[1] == "Removed: -9, -8."
    assert "Gone | -1 | 1" in first and "Fine | -4 | 1" in first
    assert second.splitlines()[0] == "Bot started. Chats removed on check: 0."
    assert "Removed:" not in second

    async with db.transaction() as c:
        await services.set_user_lang(c, 10, "ru")
    transport.calls.clear()
    await entry.send_startup_report(ctx, [])
    assert transport.calls[0]["text"].startswith("\u0411\u043e\u0442 \u0437\u0430\u043f\u0443\u0449\u0435\u043d. \u0421\u043d\u044f\u0442\u043e \u0447\u0430\u0442\u043e\u0432 \u043f\u0440\u0438 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0435: 0.")


async def test_no_report_without_root_or_without_private_contact(db):
    ctx, services, transport, _c = mk_ctx(db)
    await entry.send_startup_report(ctx, [])  # no root at all
    await make_root(db, services, 10, contact=False)
    await entry.send_startup_report(ctx, [])
    assert transport.calls == []


async def test_report_send_failure_does_not_crash(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, 10, contact=True)
    transport.queue_raises([PermanentSend()])
    await entry.send_startup_report(ctx, [])


async def test_prune_removes_only_records_older_than_two_days(db):
    clock = FakeClock()
    ctx, services, transport, _c = mk_ctx(db, clock)
    async with db.transaction() as c:
        await services.claim_update(c, BOT_ID, 1)
    clock.advance(3 * 86400)
    async with db.transaction() as c:
        await services.claim_update(c, BOT_ID, 2)
    clock.advance(3600)
    assert await entry.prune_once(ctx) == 1
    async with db.reader() as c:
        cur = await c.execute("SELECT update_id FROM processed_updates")
        assert await cur.fetchall() == [(2,)]


# --- serve: exit policy, pauses, loops ---


class RecordingClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


async def _serve(db, bot, clock, stop, transport=None):
    return await asyncio.wait_for(
        entry.serve(
            db=db, bot=bot, transport=transport or RecordingTransport(), bot_id=BOT_ID,
            bot_username="upb_bot", cooldown=0.0, clock=clock, stop=stop,
        ),
        5,
    )


async def test_revoked_token_is_critical_and_exit_3(db, caplog):
    clock, stop = RecordingClock(), asyncio.Event()
    bot = FakeBot([aex.TelegramUnauthorizedError(method=None, message=MARKER)])
    with caplog.at_level(logging.DEBUG):
        assert await _serve(db, bot, clock, stop) == 3
    assert 60.0 not in clock.sleeps  # the pause belongs to the caller, after the resources close
    crit = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(crit) == 1 and MARKER not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


async def test_ten_poll_failures_are_an_error_and_exit_1(db, caplog):
    clock, stop = RecordingClock(), asyncio.Event()
    bot = FakeBot([RuntimeError(MARKER)] * 10)
    with caplog.at_level(logging.DEBUG):
        assert await _serve(db, bot, clock, stop) == 1
    assert clock.sleeps.count(5.0) >= 9 and 60.0 not in clock.sleeps
    assert any(r.levelno == logging.ERROR and "polling_failed" in r.getMessage() for r in caplog.records)
    assert MARKER not in caplog.text


async def test_unauthorized_from_the_outbox_is_fatal_too(db):
    clock, stop = RecordingClock(), asyncio.Event()
    ctx, services, _t, _c = mk_ctx(db, clock)
    async with db.transaction() as c:
        await services.queue_event(
            c, event_key="k", event_type="chat_farewell", target_kind="chat", target_id=-1,
            generation=1, payload={"lang": "en"},
        )
    transport = RecordingTransport()
    transport.queue_raises([Unauthorized()])
    class Slow(FakeBot):
        async def get_updates(self, **kw):
            await asyncio.Event().wait()
    assert await _serve(db, Slow([]), clock, stop, transport) == 3


async def test_clean_stop_exits_0_without_a_pause(db):
    clock, stop = RecordingClock(), asyncio.Event()
    bot = FakeBot([[_bare(1)]], stop)
    assert await _serve(db, bot, clock, stop) == 0
    assert 60.0 not in clock.sleeps


async def test_serve_runs_startup_before_polling(db):
    clock, stop = RecordingClock(), asyncio.Event()
    ctx, services, _t, _c = mk_ctx(db, clock)
    await _reg_world(db, services)
    transport = RecordingTransport()
    transport.set_probe(-1, PermanentSend())
    bot = FakeBot([], stop)
    assert await _serve(db, bot, clock, stop, transport) == 0
    assert sorted(transport.probes) == [-4, -3, -2, -1]
    report = [c for c in transport.calls if c["chat_id"] == 10]
    assert report and "Removed: -1." in report[0]["text"]
    assert bot.offsets[0] is None


async def test_sigterm_sets_stop_and_interrupts_waits(db):
    stop, signalled = asyncio.Event(), asyncio.Event()
    entry._install_stop_handlers(stop, signalled)
    try:
        task = asyncio.create_task(entry.wait_or_stop(FakeClockNeverSleeps(), stop, 3600))
        await asyncio.sleep(0.01)
        signal.raise_signal(signal.SIGTERM)
        assert await asyncio.wait_for(task, 2) is True
        assert signalled.is_set()
    finally:
        loop = asyncio.get_running_loop()
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)


class FakeClockNeverSleeps(FakeClock):
    async def sleep(self, seconds):
        await asyncio.Event().wait()


# --- _run: Vault failure, session and database closing, logging hygiene ---


def _cfg(tmp_path):
    return Config(
        vault_addr="https://v", vault_role_id="r", vault_secret_id="s", vault_secret_path="p",
        db_path=str(tmp_path / "run.sqlite3"),
    )


async def test_vault_error_at_start_pauses_60_seconds_and_exits_1(tmp_path, monkeypatch, caplog):
    sleeps = []
    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(entry.SYSTEM_CLOCK.__class__, "sleep", lambda self, s: fake_sleep(s))
    monkeypatch.setattr(entry.config_module, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry, "_install_stop_handlers", lambda stop, signalled: None)
    monkeypatch.setattr(entry, "configure_logging", lambda level: None)

    def bad_vault(cfg):
        raise entry.vault.VaultError(f"login failed {MARKER}")

    monkeypatch.setattr(entry.vault, "load_bot_token", bad_vault)
    with caplog.at_level(logging.DEBUG):
        assert await entry._run() == 1
    assert sleeps == [60.0]
    assert MARKER not in caplog.text


async def test_session_and_database_are_closed_on_exit(tmp_path, monkeypatch):
    closed = {"session": False}
    dbs = []

    class Session:
        async def close(self):
            closed["session"] = True

    class FakeAiogramBot(FakeBot):
        def __init__(self, token, default=None):
            super().__init__([])
            self.session = Session()
            self.id = BOT_ID

        async def get_me(self):
            return type("Me", (), {"id": BOT_ID, "username": "upb_bot"})()

    real_open = entry.open_database

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(path):
        async with real_open(path) as database:
            dbs.append(database)
            yield database

    monkeypatch.setattr(entry, "Bot", FakeAiogramBot)
    monkeypatch.setattr(entry, "open_database", spy)
    stop = asyncio.Event()
    stop.set()
    assert await entry._serve_with_token(_cfg(tmp_path), "123:tok", stop, asyncio.Event()) == 0
    assert closed["session"] is True
    assert dbs and dbs[0]._conn is None


class _RevokedTokenBot(FakeBot):
    """aiogram Bot stand-in whose first getUpdates reports a revoked token."""

    def __init__(self, events, token=None, default=None):
        super().__init__([aex.TelegramUnauthorizedError(method=None, message=MARKER)])
        self.id = BOT_ID
        outer = events

        class Session:
            async def close(self):
                outer.append("session closed")

        self.session = Session()

    async def get_me(self):
        return type("Me", (), {"id": BOT_ID, "username": "upb_bot"})()


def _spy_open_database(monkeypatch, events, dbs):
    from contextlib import asynccontextmanager

    real_open = entry.open_database

    @asynccontextmanager
    async def spy(path):
        async with real_open(path) as database:
            dbs.append(database)
            yield database
        events.append("db closed")

    monkeypatch.setattr(entry, "open_database", spy)


async def test_fatal_pause_starts_only_after_the_database_and_session_are_closed(
    tmp_path, monkeypatch
):
    events, dbs = [], []
    _spy_open_database(monkeypatch, events, dbs)
    monkeypatch.setattr(entry, "Bot", lambda token, default=None: _RevokedTokenBot(events))

    async def fake_sleep(self, seconds):
        if seconds != entry.FATAL_PAUSE:
            await asyncio.Event().wait()  # loop waits: cancelled at stop
        events.append(f"pause {seconds}")

    monkeypatch.setattr(entry.SYSTEM_CLOCK.__class__, "sleep", fake_sleep)
    code = await asyncio.wait_for(
        entry._serve_with_token(_cfg(tmp_path), "123:tok", asyncio.Event(), asyncio.Event()), 5
    )
    assert code == 3
    assert events == ["db closed", "session closed", "pause 60.0"]


async def test_sigterm_during_the_fatal_pause_exits_at_once_with_resources_closed(
    tmp_path, monkeypatch
):
    events, dbs = [], []
    _spy_open_database(monkeypatch, events, dbs)
    monkeypatch.setattr(entry, "Bot", lambda token, default=None: _RevokedTokenBot(events))
    stop, signalled = asyncio.Event(), asyncio.Event()
    entry._install_stop_handlers(stop, signalled)  # real signal handlers, as in production
    loop = asyncio.get_running_loop()
    try:
        task = asyncio.create_task(
            entry._serve_with_token(_cfg(tmp_path), "123:tok", stop, signalled)
        )
        for _ in range(200):  # wait until the pause has begun
            if events == ["db closed", "session closed"]:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert events == ["db closed", "session closed"]
        # serve() sets `stop` itself while winding down; that must not end the pause
        assert stop.is_set() and not signalled.is_set()
        assert not task.done()

        started = loop.time()
        signal.raise_signal(signal.SIGTERM)
        assert await asyncio.wait_for(task, 2) == 3
        assert loop.time() - started < 1.0
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)
    assert dbs[0]._conn is None


def test_debug_logging_does_not_emit_sql_with_personal_data(tmp_path, caplog):
    import asyncio as aio

    from app.db import Database, apply_migrations
    from app.services import Services

    names = ("aiosqlite", "aiohttp", "aiogram", "urllib3", "requests", "")
    saved = {n: logging.getLogger(n).level for n in names}

    async def workload():
        database = Database(str(tmp_path / "debug.sqlite3"))
        await database.connect()
        await apply_migrations(database)
        try:
            async with database.transaction() as c:
                await Services().touch_user(
                    c, 42, username="ivan_private", display_name="Ivan Private-Name"
                )
                await Services().register_chat(c, -7, "Secret Chat Title", 42)
        finally:
            await database.close()

    try:
        entry.configure_logging("DEBUG")
        caplog.set_level(logging.DEBUG)
        aio.run(workload())
    finally:
        for n, lvl in saved.items():
            logging.getLogger(n).setLevel(lvl)
    for private in ("ivan_private", "Ivan Private-Name", "Secret Chat Title"):
        assert private not in caplog.text


def test_third_party_loggers_are_never_below_warning():
    names = ("aiohttp", "aiogram", "aiosqlite", "urllib3", "requests")
    saved = {n: logging.getLogger(n).level for n in (*names, "")}
    try:
        entry.configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG
        for n in names:
            assert logging.getLogger(n).getEffectiveLevel() >= logging.WARNING
        entry.configure_logging("ERROR")
        for n in names:
            assert logging.getLogger(n).getEffectiveLevel() >= logging.ERROR
        entry.configure_logging("nonsense")
        assert logging.getLogger().level == logging.INFO
    finally:
        for n, lvl in saved.items():
            logging.getLogger(n).setLevel(lvl)


async def test_signal_during_vault_login_aborts_startup(tmp_path, monkeypatch):
    events = []

    def fake_login(cfg):
        for ev in events:
            ev.set()  # a signal arrives while the login is in flight
        return "123:tok"

    def boom(*a, **kw):
        raise AssertionError("startup must not continue")

    monkeypatch.setattr(entry.config_module, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.vault, "load_bot_token", fake_login)
    monkeypatch.setattr(entry, "_install_stop_handlers", lambda s, g: events.extend([s, g]))
    monkeypatch.setattr(entry, "Bot", boom)
    assert await asyncio.wait_for(entry._run(), 5) == 0


async def test_stop_before_serve_skips_reconcile_report_and_prune(db):
    clock, stop = RecordingClock(), asyncio.Event()
    ctx, services, _t, _c = mk_ctx(db, clock)
    await _reg_world(db, services)
    transport = RecordingTransport()
    stop.set()
    assert await _serve(db, FakeBot([], stop), clock, stop, transport) == 0
    assert transport.probes == [] and transport.calls == []


async def test_signal_during_reconcile_stops_probing_report_and_prune(db):
    clock, stop = RecordingClock(), asyncio.Event()
    ctx, services, _t, _c = mk_ctx(db, clock)
    await _reg_world(db, services)

    class SignalOnProbe(RecordingTransport):
        async def probe_chat(self, chat_id: int) -> None:
            await super().probe_chat(chat_id)
            stop.set()  # the signal arrives during the first probe

    transport = SignalOnProbe()
    assert await _serve(db, FakeBot([], stop), clock, stop, transport) == 0
    assert len(transport.probes) == 1
    assert transport.calls == []  # no root report
    assert 3600.0 not in clock.sleeps  # no loops were started


async def _interrupted_reconcile(db, ctx, services, transport):
    """Chat -1 is gone; the signal arrives right after it was unregistered."""
    class GoneThenSignal(type(transport)):
        async def probe_chat(self, chat_id: int) -> None:
            await super().probe_chat(chat_id)
            ctx.delivery.stop.set()
            raise PermanentSend()

    transport.__class__ = GoneThenSignal
    return await entry.reconcile_chats(ctx, transport)


async def _notices(db):
    async with db.reader() as c:
        cur = await c.execute(
            "SELECT event_key, target_id, payload FROM outbox WHERE event_type = 'reconcile_removed'"
        )
        return await cur.fetchall()


async def test_interrupted_reconcile_queues_a_root_notice_delivered_by_the_outbox(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _reg_world(db, services)
    async with db.transaction() as c:
        await services.set_user_lang(c, 10, "ru")
    removed = await _interrupted_reconcile(db, ctx, services, transport)
    assert len(removed) == 1
    gone = removed[0]
    rows = await _notices(db)
    assert len(rows) == 1 and rows[0][1] == 10
    assert rows[0][0].startswith("reconcile_removed:10:2026-01-01T00:00:00")
    await entry.reconcile_chats(ctx, transport)  # nothing left to remove: no second notice
    assert len(await _notices(db)) == 1
    ctx.delivery.stop.clear()
    transport.calls.clear()
    assert await ctx.delivery.run_outbox_once() == 1
    assert transport.calls[0]["chat_id"] == 10
    assert str(gone) in transport.calls[0]["text"] and "прервана" in transport.calls[0]["text"]


async def test_interrupted_reconcile_english_text_and_no_notice_without_root_contact(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _reg_world(db, services)
    removed = await _interrupted_reconcile(db, ctx, services, transport)
    ctx.delivery.stop.clear()
    transport.calls.clear()
    await ctx.delivery.run_outbox_once()
    assert transport.calls[0]["text"] == f"Startup check was interrupted. Chats removed before that: {removed[0]}."

    ctx2, services2, transport2, _c2 = mk_ctx(db)
    async with db.transaction() as c:
        await c.execute("DELETE FROM outbox")
        await c.execute("UPDATE users SET private_contact_at = NULL WHERE user_id = 10")
        await c.execute(
            "INSERT INTO chats(chat_id, title, registered_by, registered_at, "
            "registration_generation, lang) VALUES (-11, 'X', 1, 't', 1, 'en')"
        )
    await _interrupted_reconcile(db, ctx2, services2, transport2)
    assert await _notices(db) == []


async def test_uninterrupted_reconcile_queues_no_notice(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _reg_world(db, services)
    transport.set_probe(-1, PermanentSend())
    assert await entry.reconcile_chats(ctx, transport) == [-1]
    assert await _notices(db) == []


async def test_same_chats_removed_in_a_later_interrupted_run_are_notified_again(db):
    clock = FakeClock()
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _reg_world(db, services)
    (gone,) = await _interrupted_reconcile(db, ctx, services, transport)
    clock.advance(3600)
    async with db.transaction() as c:
        await c.execute(
            "INSERT INTO chats(chat_id, title, registered_by, registered_at, "
            "registration_generation, lang) VALUES (?, 'Again', 1, 't', 9, 'en')",
            (gone,),
        )
    ctx.delivery.stop.clear()
    assert await _interrupted_reconcile(db, ctx, services, transport) == [gone]
    keys = [r[0] for r in await _notices(db)]
    assert len(keys) == 2 and len(set(keys)) == 2


async def test_interrupted_notice_with_many_ids_is_split_into_fitting_messages(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, 10, contact=True)
    ids = [-1000000000000 - i for i in range(400)]
    await entry._queue_interrupted_notice(ctx, ids, "run-1")
    rows = await _notices(db)
    assert len(rows) > 1
    assert await ctx.delivery.run_outbox_once() == len(rows)
    texts = [c["text"] for c in transport.calls]
    assert all(len(t) <= rendering.MAX_MESSAGE for t in texts)
    assert sum(str(i) in "".join(texts) for i in ids) == len(ids)  # every id is reported once
    assert "".join(texts).count("-1000000000399") == 1


def test_startup_report_splits_a_long_removed_line():
    ids = [-1000000000000 - i for i in range(400)]
    parts = rendering.startup_report_text(ids, [], "en")
    assert len(parts) > 1
    assert all(len(p) <= rendering.MAX_MESSAGE for p in parts)
    body = "\n".join(parts)
    assert all(str(i) in body for i in ids)


async def test_failed_notice_write_during_signalled_shutdown_still_exits_zero(db, monkeypatch, caplog):
    clock, stop = RecordingClock(), asyncio.Event()
    ctx, services, _t, _c = mk_ctx(db, clock)
    await _reg_world(db, services)

    async def broken(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(entry, "_queue_interrupted_notice", broken)

    class GoneThenSignal(RecordingTransport):
        async def probe_chat(self, chat_id: int) -> None:
            await super().probe_chat(chat_id)
            stop.set()
            raise PermanentSend()

    with caplog.at_level(logging.ERROR):
        assert await _serve(db, FakeBot([], stop), clock, stop, GoneThenSignal()) == 0
    assert "interrupted_notice_error exc=RuntimeError" in caplog.text


async def _serve_with_gone_chat(db, send_fails, chats_gone=True):
    clock, stop = RecordingClock(), asyncio.Event()
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _reg_world(db, services)
    if chats_gone:
        transport.set_probe(-1, PermanentSend())
    if send_fails:
        transport.queue_raises([PermanentSend()])
    assert await _serve(db, FakeBot([[_bare(1)]], stop), clock, stop, transport) == 0
    return await _notices(db)


async def test_lost_startup_report_queues_the_removed_chats_notice(db):
    rows = await _serve_with_gone_chat(db, send_fails=True)
    assert len(rows) == 1 and rows[0][1] == 10
    payload = json.loads(rows[0][2])
    assert payload["chat_ids"] == [-1] and payload["report_lost"] is True


async def test_delivered_startup_report_queues_no_notice(db):
    assert await _serve_with_gone_chat(db, send_fails=False) == []


async def test_lost_startup_report_without_removed_chats_queues_no_notice(db):
    assert await _serve_with_gone_chat(db, send_fails=True, chats_gone=False) == []


async def test_startup_report_returns_whether_everything_was_delivered(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, 10, contact=True)
    assert await entry.send_startup_report(ctx, []) == (True, 0)
    transport.queue_raises([PermanentSend()])
    assert await entry.send_startup_report(ctx, []) == (False, 0)
    assert await entry.send_startup_report(ctx, [-1]) == (True, 1)


class _FailNthSend(RecordingTransport):
    """Fails the n-th send (1-based) with exc; `on_fail` runs first."""

    def __init__(self, n, exc, on_fail=None):
        super().__init__()
        self._n, self._exc, self._on_fail = n, exc, on_fail

    async def send_message(self, chat_id, text, **kw):
        await super().send_message(chat_id, text, **kw)
        if len(self.calls) == self._n:
            if self._on_fail:
                self._on_fail()
            raise self._exc


async def _serve_with_long_report(db, transport_factory, stop=None, lang="en"):
    """Many chats so the report has several parts; chat -1 is gone."""
    clock, stop = RecordingClock(), stop or asyncio.Event()
    ctx, services, _t, _c = mk_ctx(db, clock)
    await make_root(db, services, 10, contact=True)
    async with db.transaction() as c:
        await c.execute("UPDATE users SET lang = ? WHERE user_id = 10", (lang,))
    for n in range(1, 260):
        await register_chat(db, services, -n, 10, f"Chat number {n} " + "x" * 20)
    transport = transport_factory(stop)
    transport.set_probe(-1, PermanentSend())
    assert await _serve(db, FakeBot([[_bare(1)]], stop), clock, stop, transport) == 0
    return transport, await _notices(db)


async def test_report_lost_after_the_ids_part_queues_no_notice(db):
    transport, rows = await _serve_with_long_report(
        db, lambda stop: _FailNthSend(2, PermanentSend())
    )
    assert len(transport.calls) == 2 and "-1" in transport.calls[0]["text"]  # ids in part one
    assert rows == []


async def _serve_with_many_removed(db, fail_at, lang="en"):
    """400 removed chats (two ids groups, one report part each) and a failing n-th send."""
    clock, stop = RecordingClock(), asyncio.Event()
    ctx, services, _t, _c = mk_ctx(db, clock)
    await make_root(db, services, 10, contact=True)
    async with db.transaction() as c:
        await c.execute("UPDATE users SET lang = ? WHERE user_id = 10", (lang,))
    removed = [-(10**12) - n for n in range(400)]
    for chat_id in removed:
        await register_chat(db, services, chat_id, 10, "g")
    transport = _FailNthSend(fail_at, PermanentSend())
    for chat_id in removed:
        transport.set_probe(chat_id, PermanentSend())
    assert await _serve(db, FakeBot([[_bare(1)]], stop), clock, stop, transport) == 0
    return removed, transport, await _notices(db)


def _ids_in(text):
    return {int(w.strip(",.")) for w in text.split() if w.strip(",.").startswith("-10")}


async def test_second_ids_part_lost_queues_only_the_undelivered_groups(db):
    removed, transport, rows = await _serve_with_many_removed(db, fail_at=2)
    (row,) = rows
    payload = json.loads(row[2])
    delivered = _ids_in(transport.calls[0]["text"])
    assert payload["report_partial"] is True
    assert delivered and not delivered & set(payload["chat_ids"])
    assert delivered | set(payload["chat_ids"]) == set(removed)  # nothing lost, nothing twice


async def test_first_ids_part_lost_queues_every_group_as_lost(db):
    removed, _transport, rows = await _serve_with_many_removed(db, fail_at=1)
    payloads = [json.loads(r[2]) for r in rows]
    assert len(payloads) == 2
    assert {i for p in payloads for i in p["chat_ids"]} == set(removed)
    assert all(p["report_lost"] is True and p["report_partial"] is False for p in payloads)


async def test_report_partial_notice_is_rendered_in_the_root_language(db):
    _removed, _transport, rows = await _serve_with_many_removed(db, fail_at=2, lang="ru")
    ids = json.loads(rows[0][2])["chat_ids"]
    ctx, _s, transport, _c = mk_ctx(db, FakeClock())
    await ctx.delivery.run_outbox_once()
    text = transport.calls[0]["text"]
    assert text == rendering.report_partial_text(ids, "ru")
    assert text.startswith("\u0427\u0430\u0441\u0442\u044c \u043e\u0442\u0447\u0451\u0442\u0430")


def test_report_partial_text_in_english():
    assert rendering.report_partial_text([-1, -2]) == (
        "Part of the startup report was not delivered. "
        "Chats removed on check and not reported: -1, -2."
    )


async def test_report_lost_before_the_ids_part_queues_the_report_lost_notice(db):
    transport, rows = await _serve_with_long_report(
        db, lambda stop: _FailNthSend(1, PermanentSend()), lang="ru"
    )
    (row,) = rows
    assert json.loads(row[2])["report_lost"] is True
    clock = FakeClock()
    ctx, _s, transport2, _c = mk_ctx(db, clock)
    await ctx.delivery.run_outbox_once()
    assert transport2.calls[0]["text"] == (
        "\u041e\u0442\u0447\u0451\u0442 \u043e \u0437\u0430\u043f\u0443\u0441\u043a\u0435 "
        "\u043d\u0435 \u0434\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d. "
        "\u0421\u043d\u044f\u0442\u043e \u0447\u0430\u0442\u043e\u0432 \u043f\u0440\u0438 "
        "\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0435: -1."
    )


async def test_report_lost_text_in_english(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, 10, contact=True)
    async with db.transaction() as c:
        await c.execute("UPDATE users SET lang = 'en' WHERE user_id = 10")
    await entry._queue_interrupted_notice(ctx, [-1, -2], "t", report_lost=True)
    await ctx.delivery.run_outbox_once()
    assert transport.calls[0]["text"] == (
        "Startup report was not delivered. Chats removed on check: -1, -2."
    )


async def test_stop_during_the_report_429_wait_still_queues_the_notice(db):
    stop = asyncio.Event()
    transport, rows = await _serve_with_long_report(
        db, lambda s: _FailNthSend(1, RateLimited(3600.0), on_fail=s.set), stop=stop
    )
    assert len(transport.calls) == 1  # no retry after the stop
    (row,) = rows
    assert json.loads(row[2])["chat_ids"] == [-1]


async def test_prune_once_drops_old_finished_outbox_rows_but_never_pending(db):
    clock = FakeClock()
    ctx, services, _t, _c = mk_ctx(db, clock)
    async with db.transaction() as c:
        for n, status in enumerate(("sent", "failed", "cancelled", "pending")):
            await services.queue_event(
                c, event_key=f"old{n}", event_type="chat_farewell", target_kind="chat",
                target_id=-n - 1, generation=n, payload={"lang": "en"},
            )
            await c.execute("UPDATE outbox SET status = ? WHERE event_key = ?", (status, f"old{n}"))
    clock.advance(31 * 86400)
    async with db.transaction() as c:
        await services.queue_event(
            c, event_key="fresh", event_type="chat_farewell", target_kind="chat",
            target_id=-9, generation=9, payload={"lang": "en"},
        )
        await c.execute("UPDATE outbox SET status = 'sent' WHERE event_key = 'fresh'")
    await entry.prune_once(ctx)
    async with db.reader() as c:
        cur = await c.execute("SELECT event_key FROM outbox ORDER BY event_key")
        assert [r[0] for r in await cur.fetchall()] == ["fresh", "old3"]
