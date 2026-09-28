"""Recorded processed_updates outcome of every command path, the ping cooldown clock
and the 'error' upsert (decisions sections 2.5, 3 and 8.1)."""

import pytest

from app.handlers import handle_event
from app.rendering import t

from conftest import (
    BOT_ID,
    group_event,
    make_admin,
    make_root,
    mk_ctx,
    private_event,
    register_chat,
    subscribe,
)

CHAT = 500
ROOT = 10
OWNER = 1
SUB = 20
NOBODY = 40


async def _outcome(db, update_id: int) -> str | None:
    async with db.reader() as c:
        cur = await c.execute(
            "SELECT outcome FROM processed_updates WHERE bot_id = ? AND update_id = ?",
            (BOT_ID, update_id),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def _group_world(db, services):
    await make_root(db, services, ROOT)
    await make_admin(db, services, OWNER)
    await register_chat(db, services, CHAT, OWNER)
    await subscribe(db, services, CHAT, SUB)


# (sender, text, expected outcome). Only silent paths are 'ignored'.
GROUP_PATHS = [
    (ROOT, "/upb chat register", "ok"),  # "already registered" reply
    (OWNER, "/upb chat unregister", "ok"),  # farewell goes through the outbox
    (NOBODY, "/upb notify on", "ok"),
    (SUB, "/upb notify off", "ok"),
    (NOBODY, "/upb notify off", "ignored"),  # denied
    (SUB, "/upb all", "ok"),  # pings the others or answers pong
    (SUB, "/upb list", "ok"),
    (SUB, "/upb help", "ok"),
    (OWNER, "/upb lang ru", "ok"),
    (OWNER, "/upb lang de", "ok"),  # bad_args reply
    (SUB, "/upb lang ru", "ignored"),  # denied
    (SUB, "/upb", "ok"),  # partial help
    (NOBODY, "/upb qwe", "ok"),
    (SUB, "/upb chat", "ignored"),  # known prefix, nothing allowed under it: silence
    (NOBODY, "/upb all", "ignored"),  # denied
]


@pytest.mark.parametrize("sender,text,expected", GROUP_PATHS)
async def test_group_outcome(db, sender, text, expected):
    ctx, services, transport, _c = mk_ctx(db)
    await _group_world(db, services)
    await handle_event(ctx, group_event(text, update_id=1, user_id=sender, chat_id=CHAT))
    assert await _outcome(db, 1) == expected
    if expected == "ok":
        assert transport.calls or await _outbox_rows(db)


async def _outbox_rows(db) -> int:
    async with db.reader() as c:
        cur = await c.execute("SELECT COUNT(*) FROM outbox")
        return (await cur.fetchone())[0]


async def test_group_register_in_a_free_chat_is_ok_and_denied_is_ignored(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_admin(db, services, OWNER)
    await handle_event(ctx, group_event("/upb chat register", update_id=1, user_id=NOBODY))
    await handle_event(ctx, group_event("/upb chat register", update_id=2, user_id=OWNER))
    assert await _outcome(db, 1) == "ignored"
    assert await _outcome(db, 2) == "ok"


async def test_successful_unregister_is_ok_and_leaves_a_farewell(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _group_world(db, services)
    await handle_event(ctx, group_event("/upb chat unregister", update_id=1, user_id=OWNER, chat_id=CHAT))
    assert await _outcome(db, 1) == "ok"
    assert await _outbox_rows(db) == 1


async def test_event_from_an_old_migrated_id_is_ignored(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _group_world(db, services)
    async with db.transaction() as c:
        await services.migrate_chat(c, CHAT, -1001)
    await handle_event(ctx, group_event("/upb all", update_id=1, user_id=SUB, chat_id=CHAT))
    assert await _outcome(db, 1) == "ignored"
    assert transport.calls == []


async def test_rate_limited_ping_is_ignored_and_the_first_is_ok(db):
    ctx, services, transport, _c = mk_ctx(db, cooldown=5.0)
    await _group_world(db, services)
    await handle_event(ctx, group_event("/upb all", update_id=1, user_id=SUB, chat_id=CHAT))
    await handle_event(ctx, group_event("/upb all", update_id=2, user_id=SUB, chat_id=CHAT))
    assert (await _outcome(db, 1), await _outcome(db, 2)) == ("ok", "ignored")
    assert len(transport.calls) == 1


# --- ping cooldown vs. a wall clock that goes backwards ---


async def test_cooldown_holds_within_the_window(db):
    ctx, services, transport, clock = mk_ctx(db, cooldown=5.0)
    await _group_world(db, services)
    await handle_event(ctx, group_event("/upb all", update_id=1, user_id=SUB, chat_id=CHAT))
    clock.advance(4)
    await handle_event(ctx, group_event("/upb all", update_id=2, user_id=SUB, chat_id=CHAT))
    clock.advance(1)
    await handle_event(ctx, group_event("/upb all", update_id=3, user_id=SUB, chat_id=CHAT))
    assert [await _outcome(db, i) for i in (1, 2, 3)] == ["ok", "ignored", "ok"]


async def test_clock_moved_back_counts_the_cooldown_as_expired(db):
    ctx, services, transport, clock = mk_ctx(db, cooldown=5.0)
    await _group_world(db, services)
    await handle_event(ctx, group_event("/upb all", update_id=1, user_id=SUB, chat_id=CHAT))
    clock.advance(-3600)  # NTP step
    await handle_event(ctx, group_event("/upb all", update_id=2, user_id=SUB, chat_id=CHAT))
    clock.advance(1)
    await handle_event(ctx, group_event("/upb all", update_id=3, user_id=SUB, chat_id=CHAT))
    assert [await _outcome(db, i) for i in (1, 2, 3)] == ["ok", "ok", "ignored"]
    assert len(transport.calls) == 2


# --- private paths ---


async def _private_world(db, services):
    await make_root(db, services, ROOT, contact=True)
    await make_admin(db, services, OWNER)


PRIVATE_PATHS = [
    (ROOT, "/help", "ok"),
    (OWNER, "/start", "ok"),
    (OWNER, "/usage", "ok"),
    (ROOT, "/lang ru", "ok"),
    (ROOT, "/lang de", "ok"),  # bad_args reply
    (ROOT, "/admin create 55", "ok"),
    (ROOT, "/admin create x", "ok"),  # bad_args reply
    (ROOT, "/admin create 10", "ok"),  # root_cli_only reply
    (ROOT, "/admin remove 1", "ok"),
    (ROOT, "/admin remove 777", "ok"),  # admin_absent reply
    (ROOT, "/admin list", "ok"),
    (OWNER, "/chat list", "ok"),
    (ROOT, "/chat list", "ok"),
    (ROOT, "/chat remove 123", "ok"),  # chat_absent reply
    (ROOT, "/admin", "ok"),  # partial help
    (ROOT, "/chat", "ok"),
    (OWNER, "/admin", "ignored"),  # nothing allowed under the prefix
    (OWNER, "/admin list", "ignored"),  # denied
    (OWNER, "/chat remove 123", "ignored"),  # denied
    (NOBODY, "/help", "ignored"),  # denied
    (ROOT, "hello there", "ignored"),  # not a command
]


@pytest.mark.parametrize("sender,text,expected", PRIVATE_PATHS)
async def test_private_outcome(db, sender, text, expected):
    ctx, services, transport, _c = mk_ctx(db)
    await _private_world(db, services)
    await handle_event(ctx, private_event(text, update_id=1, user_id=sender))
    assert await _outcome(db, 1) == expected
    assert bool(transport.calls) == (expected == "ok")


async def test_private_chat_remove_of_a_real_chat_is_ok(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _private_world(db, services)
    await register_chat(db, services, CHAT, OWNER)
    await handle_event(ctx, private_event(f"/chat remove {CHAT}", update_id=1, user_id=ROOT))
    assert await _outcome(db, 1) == "ok"
    assert transport.calls[0]["text"] == t("chat_removed", "en", chat_id=CHAT)


# --- error is recorded whether or not the claim row survived ---


async def test_error_before_commit_is_inserted_because_the_claim_rolled_back(db, monkeypatch):
    ctx, services, transport, _c = mk_ctx(db)
    await _group_world(db, services)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "subscribe", boom)
    await handle_event(ctx, group_event("/upb notify on", update_id=1, user_id=NOBODY, chat_id=CHAT))
    assert await _outcome(db, 1) == "error"
    assert transport.calls == []


async def test_error_after_commit_overwrites_the_recorded_ok(db, monkeypatch):
    ctx, services, transport, _c = mk_ctx(db)
    await _group_world(db, services)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ctx.delivery, "send_reply", boom)
    await handle_event(ctx, group_event("/upb notify on", update_id=1, user_id=NOBODY, chat_id=CHAT))
    assert await _outcome(db, 1) == "error"
    async with db.reader() as c:
        assert await services.is_subscribed(c, CHAT, NOBODY)  # the committed effect stays


async def test_record_update_outcome_upserts(db):
    ctx, services, *_ = mk_ctx(db)
    async with db.transaction() as c:
        await services.record_update_outcome(c, BOT_ID, 7, "error")  # no row yet
    assert await _outcome(db, 7) == "error"
    async with db.transaction() as c:
        assert await services.claim_update(c, BOT_ID, 8)
        await services.record_update_outcome(c, BOT_ID, 8, "error")  # row exists
    assert await _outcome(db, 8) == "error"
