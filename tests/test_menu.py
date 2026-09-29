"""Per-chat command menu: set for registered chats, delete otherwise, never crashing."""

import asyncio
import logging

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

import app.__main__ as entry
from app import rendering
from app.delivery import AmbiguousSend, ChatMigrated, PermanentSend, RateLimited
from app.handlers import handle_event
from app.telegram import AiogramTransport

from conftest import (
    BOT_ID,
    FakeClock,
    group_event,
    make_admin,
    make_event,
    make_root,
    mk_ctx,
    private_event,
    register_chat,
    subscribe,
)
from test_polling import FakeBot
from test_transport_aiogram import CHAT, TOKEN, ScriptedSession

ROOT = 10
ADMIN = 1
CHAT_ID = 500
COMMANDS = ["all", "on", "off", "help", "usage"]


def _names(call):
    return [name for name, _desc in call["commands"]]


async def _world(db, services):
    await make_root(db, services, ROOT)
    await make_admin(db, services, ADMIN)


async def _say(ctx, text, uid, user, chat_id=CHAT_ID):
    await handle_event(ctx, group_event(text, update_id=uid, user_id=user, chat_id=chat_id))


# --- handlers ---


async def test_register_sets_five_commands_in_the_chat_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/upb chat register", 1, ADMIN)
    assert len(transport.menu_calls) == 1
    call = transport.menu_calls[0]
    assert call["op"] == "set" and call["chat_id"] == CHAT_ID
    assert _names(call) == COMMANDS
    assert call["commands"] == rendering.menu_commands("en")
    assert len(transport.calls) == 1  # the welcome reply is still sent


async def test_lang_change_sets_the_menu_again_in_the_new_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/upb chat register", 1, ADMIN)
    await _say(ctx, "/upb lang ru", 2, ADMIN)
    assert [c["op"] for c in transport.menu_calls] == ["set", "set"]
    assert transport.menu_calls[1]["commands"] == rendering.menu_commands("ru")
    assert transport.menu_calls[1]["commands"] != transport.menu_calls[0]["commands"]


async def test_menu_descriptions_fit_the_telegram_limits():
    for lang in ("en", "ru"):
        for name, desc in rendering.menu_commands(lang):
            assert 1 <= len(name) <= 32 and 3 <= len(desc) <= 256


async def test_group_unregister_deletes_the_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await _say(ctx, "/upb chat unregister", 1, ADMIN)
    assert transport.menu_calls == [
        {"op": "delete", "chat_id": CHAT_ID, "commands": None}
    ]


async def test_private_chat_remove_deletes_the_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await handle_event(
        ctx, private_event(f"/chat remove {CHAT_ID}", update_id=1, user_id=ROOT)
    )
    assert [(c["op"], c["chat_id"]) for c in transport.menu_calls] == [("delete", CHAT_ID)]


async def test_admin_revoke_cascade_deletes_every_dropped_chat(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, 501, ADMIN)
    await register_chat(db, services, 502, ADMIN)
    await handle_event(
        ctx, private_event(f"/admin remove {ADMIN}", update_id=1, user_id=ROOT)
    )
    assert sorted((c["op"], c["chat_id"]) for c in transport.menu_calls) == [
        ("delete", 501),
        ("delete", 502),
    ]


async def test_bot_kicked_deletes_the_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await handle_event(
        ctx,
        make_event(
            kind="my_chat_member", update_id=1, chat_id=CHAT_ID, user_id=ROOT,
            bot_removed=True, left_user_id=BOT_ID, text=None,
        ),
    )
    assert [(c["op"], c["chat_id"]) for c in transport.menu_calls] == [("delete", CHAT_ID)]


async def test_bot_left_message_deletes_the_menu_and_a_second_one_is_silent(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    for uid in (1, 2):
        await handle_event(
            ctx,
            make_event(
                kind="member_left", update_id=uid, chat_id=CHAT_ID, user_id=None,
                left_user_id=BOT_ID, text=None,
            ),
        )
    assert [(c["op"], c["chat_id"]) for c in transport.menu_calls] == [("delete", CHAT_ID)]


async def test_member_leaving_does_not_touch_the_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await handle_event(
        ctx,
        make_event(
            kind="member_left", update_id=1, chat_id=CHAT_ID, user_id=None,
            left_user_id=77, text=None,
        ),
    )
    assert transport.menu_calls == []


@pytest.mark.parametrize("side", ["to", "from"])
async def test_migration_deletes_the_old_id_and_sets_the_new(db, side):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    new = -1001
    if side == "to":
        event = make_event(
            kind="message", update_id=1, chat_id=CHAT_ID, user_id=None, text=None,
            migrate_to_chat_id=new,
        )
    else:
        event = make_event(
            kind="message", update_id=1, chat_id=new, user_id=None, text=None,
            migrate_from_chat_id=CHAT_ID,
        )
    await handle_event(ctx, event)
    got = sorted((c["op"], c["chat_id"]) for c in transport.menu_calls)
    assert got == sorted([("delete", CHAT_ID), ("set", new)])


async def test_migration_found_by_a_failed_reply_syncs_both_ids(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.raise_next(ChatMigrated(-1001))
    await _say(ctx, "/upb list", 1, ADMIN)
    got = sorted((c["op"], c["chat_id"]) for c in transport.menu_calls)
    assert got == sorted([("delete", CHAT_ID), ("set", -1001)])


@pytest.mark.parametrize(
    "text,user",
    [
        ("/upb chat register", 40),  # nobody may register
        ("/upb lang ru", 40),
        ("/upb chat unregister", 40),
        ("/upb all", 40),
    ],
)
async def test_denied_command_makes_no_telegram_calls(db, text, user):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await _say(ctx, text, 1, user)
    assert transport.calls == [] and transport.menu_calls == []


async def test_denied_command_in_an_unregistered_chat_makes_no_calls(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/upb chat register", 1, 40)
    assert transport.calls == [] and transport.menu_calls == []


# --- errors ---


async def test_429_is_retried_at_most_three_attempts_and_waits_retry_after(db):
    clock = FakeClock()
    slept: list[float] = []
    real_sleep = clock.sleep

    async def sleep(seconds):
        slept.append(seconds)
        await real_sleep(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.fail_menu(RateLimited(7.0))
    assert await ctx.delivery.sync_chat_menu(CHAT_ID) is False
    assert len(transport.menu_calls) == 3
    assert slept == [7.0, 7.0]


async def test_429_then_success(db):
    ctx, services, transport, _c = mk_ctx(db)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.queue_menu_raises([RateLimited(1.0)])
    assert await ctx.delivery.sync_chat_menu(CHAT_ID) is True
    assert len(transport.menu_calls) == 2


async def test_stop_signal_interrupts_the_429_wait(db):
    ctx, services, transport, _c = mk_ctx(db)
    await register_chat(db, services, CHAT_ID, ADMIN)
    ctx.delivery.stop.set()
    transport.fail_menu(RateLimited(3600.0))
    assert await ctx.delivery.sync_chat_menu(CHAT_ID) is False
    assert len(transport.menu_calls) == 1


@pytest.mark.parametrize("exc", [PermanentSend(), AmbiguousSend()])
async def test_menu_failure_never_blocks_the_reply(db, exc, caplog):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    transport.fail_menu(exc)
    with caplog.at_level(logging.DEBUG):
        await _say(ctx, "/upb chat register", 1, ADMIN)
    assert len(transport.calls) == 1


async def test_delete_for_a_gone_chat_is_logged_at_info(db, caplog):
    ctx, services, transport, _c = mk_ctx(db)
    transport.fail_menu(PermanentSend())
    with caplog.at_level(logging.DEBUG):
        assert await ctx.delivery.sync_chat_menu(CHAT_ID) is False
    rec = [r for r in caplog.records if "menu_delete_permanent" in r.getMessage()]
    assert rec and all(r.levelno == logging.INFO for r in rec)


async def test_sync_is_idempotent_and_follows_db_state(db):
    ctx, services, transport, _c = mk_ctx(db)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await ctx.delivery.sync_chat_menu(CHAT_ID)
    await ctx.delivery.sync_chat_menu(CHAT_ID)
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT_ID, farewell=False)
    await ctx.delivery.sync_chat_menu(CHAT_ID)
    assert [c["op"] for c in transport.menu_calls] == ["set", "set", "delete"]


# --- startup ---


async def test_startup_sync_sets_active_and_deletes_known_inactive(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, -1, ADMIN)
    await register_chat(db, services, -2, ADMIN)
    async with db.transaction() as c:
        await services.migrate_chat(c, -2, -200)  # -2 is now a known inactive id
    await entry.sync_menus(ctx, [-9])  # removed by reconciliation
    got = {(c["chat_id"]): c["op"] for c in transport.menu_calls}
    assert got == {-1: "set", -200: "set", -2: "delete", -9: "delete"}


async def test_startup_sync_stops_early_on_the_stop_signal(db):
    ctx, services, transport, _c = mk_ctx(db)
    await register_chat(db, services, -1, ADMIN)
    await register_chat(db, services, -2, ADMIN)
    ctx.delivery.stop.set()
    await entry.sync_menus(ctx, [])
    assert transport.menu_calls == []


async def test_serve_syncs_menus_after_reconciliation(db):
    _ctx, services, transport, clock = mk_ctx(db)
    await register_chat(db, services, -1, ADMIN)
    await register_chat(db, services, -2, ADMIN)
    transport.set_probe(-2, PermanentSend())
    stop = asyncio.Event()
    code = await asyncio.wait_for(
        entry.serve(
            db=db, bot=FakeBot([], stop), transport=transport, bot_id=BOT_ID,
            bot_username="upb_bot", cooldown=0.0, clock=clock, stop=stop,
        ),
        5,
    )
    assert code == 0
    got = {c["chat_id"]: c["op"] for c in transport.menu_calls}
    assert got == {-1: "set", -2: "delete"}


# --- real transport ---


@pytest.fixture
def bot_and_session():
    session = ScriptedSession()
    bot = Bot(token=TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    return AiogramTransport(bot), session


async def test_real_transport_uses_the_chat_scope_only(bot_and_session):
    transport, session = bot_and_session
    session.replies += [(200, {"ok": True, "result": True})] * 2
    await transport.set_chat_commands(CHAT, [("all", "Ping"), ("on", "Subscribe")])
    await transport.delete_chat_commands(CHAT)
    (n1, p1), (n2, p2) = session.requests
    assert n1 == "SetMyCommands" and n2 == "DeleteMyCommands"
    assert p1["scope"] == {"type": "chat", "chat_id": CHAT}
    assert p1["commands"] == [
        {"command": "all", "description": "Ping"},
        {"command": "on", "description": "Subscribe"},
    ]
    assert p2["scope"] == {"type": "chat", "chat_id": CHAT}


async def test_real_transport_maps_errors(bot_and_session):
    transport, session = bot_and_session
    session.replies += [
        (429, {"ok": False, "error_code": 429, "description": "x", "parameters": {"retry_after": 4}}),
        (403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}),
    ]
    with pytest.raises(RateLimited) as info:
        await transport.delete_chat_commands(CHAT)
    assert info.value.retry_after == 4
    with pytest.raises(PermanentSend):
        await transport.set_chat_commands(CHAT, [("all", "Ping")])
