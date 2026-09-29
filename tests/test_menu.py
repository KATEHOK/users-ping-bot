"""Per-chat command menu: set for registered chats, delete otherwise, never crashing."""

import asyncio
import logging

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

import app.__main__ as entry
from app import rendering
from app.delivery import AmbiguousSend, ChatMigrated, PermanentSend, RateLimited, Unauthorized
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
    marker = "SECRET-TEXT"
    transport.fail_menu(type(exc)(marker))
    with caplog.at_level(logging.DEBUG):
        await _say(ctx, "/upb chat register", 1, ADMIN)
    assert len(transport.calls) == 1
    assert marker not in caplog.text
    kind = "permanent" if isinstance(exc, PermanentSend) else "ambiguous"
    assert any(r.getMessage().startswith(f"menu_set_{kind} ") for r in caplog.records)


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


# --- migrated ids ---


async def test_delete_on_an_old_migrated_id_is_done_without_migration(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    async with db.transaction() as c:
        await services.migrate_chat(c, CHAT_ID, -1001)
    transport.queue_menu_raises([ChatMigrated(-1001)])
    assert await ctx.delivery.sync_chat_menu(CHAT_ID) is True
    assert [(c["op"], c["chat_id"]) for c in transport.menu_calls] == [("delete", CHAT_ID)]


async def test_live_migration_found_by_a_menu_call_costs_three_calls(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    # set(old) and the best-effort delete(old) both answer with a migrate error
    transport.queue_menu_raises([ChatMigrated(-1001), ChatMigrated(-1001)])
    await ctx.delivery.sync_chat_menu(CHAT_ID)
    got = [(c["op"], c["chat_id"]) for c in transport.menu_calls]
    assert got == [("set", CHAT_ID), ("delete", CHAT_ID), ("set", -1001)]


async def test_live_migration_event_costs_one_delete_and_one_set(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.queue_menu_raises([ChatMigrated(-1001)])  # the delete of the old id
    event = make_event(
        kind="message", update_id=1, chat_id=CHAT_ID, user_id=None, text=None,
        migrate_to_chat_id=-1001,
    )
    await handle_event(ctx, event)
    got = [(c["op"], c["chat_id"]) for c in transport.menu_calls]
    assert got == [("delete", CHAT_ID), ("set", -1001)]


# --- fatal errors ---


async def test_unauthorized_from_a_menu_call_in_a_failed_reply_propagates(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.raise_next(ChatMigrated(-1001))
    transport.fail_menu(Unauthorized())
    with pytest.raises(Unauthorized):
        await ctx.delivery.send_reply(CHAT_ID, "x", reply_to=None, thread_id=None)


async def test_unauthorized_from_a_menu_call_in_the_outbox_keeps_the_attempt(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, CHAT_ID, ROOT)
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT_ID)  # queues a farewell
    transport.fail_menu(Unauthorized())
    with pytest.raises(Unauthorized):
        await ctx.delivery.run_outbox_once()
    async with db.reader() as c:
        cur = await c.execute("SELECT status, attempts FROM outbox WHERE event_type = 'chat_farewell'")
        assert [tuple(r) for r in await cur.fetchall()] == [("sent", 1)]  # one attempt, no replay
    assert not ctx.delivery._unwritten


async def test_unauthorized_from_the_startup_menu_sync_exits_with_code_3(db):
    _ctx, services, transport, clock = mk_ctx(db)
    await register_chat(db, services, -1, ADMIN)
    transport.fail_menu(Unauthorized())
    stop = asyncio.Event()
    code = await asyncio.wait_for(
        entry.serve(
            db=db, bot=FakeBot([], stop), transport=transport, bot_id=BOT_ID,
            bot_username="upb_bot", cooldown=0.0, clock=clock, stop=stop,
        ),
        5,
    )
    assert code == entry.EXIT_UNAUTHORIZED


# --- root change and joining ---


@pytest.mark.parametrize("fail", [None, PermanentSend()])
async def test_cli_root_change_drops_the_menu_once_the_farewell_is_done(db, fail):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, CHAT_ID, ROOT)
    async with db.transaction() as c:  # what the CLI does: no transport
        await services.touch_user(c, 11)
        await services.set_root(c, 11)
    assert transport.menu_calls == []
    if fail is not None:
        transport.fail_chat(CHAT_ID, fail)
    await ctx.delivery.run_outbox_once()
    got = [(c["op"], c["chat_id"]) for c in transport.menu_calls]
    assert got == [("delete", CHAT_ID)]


async def test_retryable_farewell_failure_does_not_touch_the_menu_yet(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, CHAT_ID, ROOT)
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT_ID)
    transport.fail_chat(CHAT_ID, AmbiguousSend())
    await ctx.delivery.run_outbox_once()
    assert transport.menu_calls == []


def _my_member(uid, *, added):
    return make_event(
        kind="my_chat_member", update_id=uid, chat_id=CHAT_ID, chat_type="supergroup",
        user_id=ADMIN, bot_added=added, text=None,
    )


async def test_bot_added_to_an_unregistered_group_deletes_a_stale_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _my_member(1, added=True))
    assert [(c["op"], c["chat_id"]) for c in transport.menu_calls] == [("delete", CHAT_ID)]
    assert transport.calls == []
    await handle_event(ctx, _my_member(1, added=True))  # a replayed update does nothing
    assert len(transport.menu_calls) == 1


async def test_other_membership_updates_do_not_sync_the_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _my_member(1, added=False))
    assert transport.menu_calls == []


def test_to_event_marks_the_bot_joining_only_on_a_status_change():
    from datetime import datetime, timezone

    from aiogram.types import (
        Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember,
        ChatMemberUpdated, Update, User,
    )
    from app.telegram import to_event

    bot = User(id=BOT_ID, is_bot=True, first_name="b")
    chat = Chat(id=CHAT_ID, type="supergroup", title="G")

    def upd(old, new):
        cmu = ChatMemberUpdated(
            chat=chat, from_user=bot, date=datetime.now(timezone.utc),
            old_chat_member=old, new_chat_member=new,
        )
        return to_event(Update(update_id=1, my_chat_member=cmu))

    left, member = ChatMemberLeft(user=bot), ChatMemberMember(user=bot)
    admin = ChatMemberAdministrator.model_construct(status="administrator", user=bot)
    assert upd(left, member).bot_added is True
    assert upd(left, admin).bot_added is True
    assert upd(member, admin).bot_added is False
    assert upd(member, left).bot_added is False


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
    assert got == {-1: "set", -200: "set", -9: "delete"}  # the migrated-away id is skipped


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


async def test_signal_during_the_startup_menu_sync_still_sends_the_report(db):
    _ctx, services, transport, clock = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, -1, ROOT)
    await register_chat(db, services, -2, ROOT)
    transport.set_probe(-2, PermanentSend())
    stop = asyncio.Event()
    real_set = transport.set_chat_commands

    async def set_and_signal(chat_id, commands):
        stop.set()  # a stop arrives while the menus are being synced
        await real_set(chat_id, commands)

    transport.set_chat_commands = set_and_signal
    code = await asyncio.wait_for(
        entry.serve(
            db=db, bot=FakeBot([], stop), transport=transport, bot_id=BOT_ID,
            bot_username="upb_bot", cooldown=0.0, clock=clock, stop=stop,
        ),
        5,
    )
    assert code == 0
    assert [c["chat_id"] for c in transport.calls] == [ROOT]  # the report went out


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
