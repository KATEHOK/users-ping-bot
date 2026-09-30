"""Per-chat command menu: set for registered chats, delete otherwise, never crashing."""

import asyncio
import logging

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

import app.__main__ as entry
from app import rendering
from app.delivery import MENU_MAX_WAIT, AmbiguousSend, ChatMigrated, PermanentSend, RateLimited, Unauthorized
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
COMMANDS = ["all", "on", "off", "list", "help", "usage"]
OWNER_COMMANDS = [*COMMANDS, "unregister", "lang"]
REGISTER_COMMANDS = ["register", "help"]


def _names(call):
    return [name for name, _desc in call["commands"]]


def _chat_ops(transport):
    """(op, chat_id) of the chat-scope calls only."""
    return [(c["op"], c["chat_id"]) for c in transport.menu_calls if c["user_id"] is None]


def _member_ops(transport):
    """{(chat_id, user_id): commands or None} of the member-scope calls (the last one wins)."""
    return {
        (c["chat_id"], c["user_id"]): c["commands"]
        for c in transport.menu_calls
        if c["user_id"] is not None
    }


async def _world(db, services):
    await make_root(db, services, ROOT)
    await make_admin(db, services, ADMIN)


async def _say(ctx, text, uid, user, chat_id=CHAT_ID):
    await handle_event(ctx, group_event(text, update_id=uid, user_id=user, chat_id=chat_id))


# --- handlers ---


async def test_register_sets_the_chat_menu_in_the_chat_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/upb chat register", 1, ADMIN)
    call = transport.menu_calls[0]
    assert call["op"] == "set" and call["chat_id"] == CHAT_ID and call["user_id"] is None
    assert _names(call) == COMMANDS
    assert call["commands"] == rendering.menu_commands("en")
    assert len(transport.calls) == 1  # the welcome reply is still sent


async def test_register_gives_root_and_the_registrar_the_owner_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/register", 1, ADMIN)
    owners = _member_ops(transport)
    assert set(owners) == {(CHAT_ID, ROOT), (CHAT_ID, ADMIN)}
    for commands in owners.values():
        assert [n for n, _ in commands] == OWNER_COMMANDS
        assert commands == rendering.owner_menu_commands("en")
    assert len(transport.menu_calls) == 3  # chat scope + two owners


async def test_root_registering_is_not_given_the_menu_twice(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/register", 1, ROOT)
    assert [(c["op"], c["user_id"]) for c in transport.menu_calls] == [("set", None), ("set", ROOT), ("delete", ADMIN)]


async def test_a_member_who_is_not_in_the_chat_is_logged_not_raised(db, caplog):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    real = transport.set_chat_commands

    async def set_commands(chat_id, commands, *, user_id=None):
        await real(chat_id, commands, user_id=user_id)
        if user_id == ROOT:
            raise PermanentSend()  # user not found in the chat

    transport.set_chat_commands = set_commands
    with caplog.at_level(logging.DEBUG):
        await _say(ctx, "/register", 1, ADMIN)
    assert (CHAT_ID, ADMIN) in _member_ops(transport)  # the others are still served
    assert any(r.getMessage().startswith("menu_set_permanent ") for r in caplog.records)
    assert len(transport.calls) == 1


async def test_lang_change_sets_the_menu_again_in_the_new_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/upb chat register", 1, ADMIN)
    await _say(ctx, "/upb lang ru", 2, ADMIN)
    first = len(transport.menu_calls)
    await _say(ctx, "/lang ru", 3, ADMIN)  # the short form syncs too
    new = transport.menu_calls[first:]
    assert {(c["user_id"]) for c in new} == {None, ROOT, ADMIN}
    for c in new:
        want = rendering.menu_commands("ru") if c["user_id"] is None else rendering.owner_menu_commands("ru")
        assert c["commands"] == want
    assert rendering.menu_commands("ru") != rendering.menu_commands("en")


async def test_menu_descriptions_fit_the_telegram_limits():
    for lang in ("en", "ru"):
        for build in (
            rendering.menu_commands, rendering.owner_menu_commands, rendering.register_menu_commands
        ):
            for name, desc in build(lang):
                assert 1 <= len(name) <= 32 and 3 <= len(desc) <= 256 and name == name.lower()


async def test_menu_sets_by_rights():
    for lang in ("en", "ru"):
        assert [n for n, _ in rendering.menu_commands(lang)] == COMMANDS
        assert [n for n, _ in rendering.owner_menu_commands(lang)] == OWNER_COMMANDS
        assert [n for n, _ in rendering.register_menu_commands(lang)] == REGISTER_COMMANDS


async def test_group_unregister_deletes_the_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await _say(ctx, "/upb chat unregister", 1, ADMIN)
    assert transport.menu_calls[0] == {
        "op": "delete", "chat_id": CHAT_ID, "user_id": None, "commands": None
    }
    # both owners are still staff: they get the register menu, the chat is free again
    assert _member_ops(transport) == {
        (CHAT_ID, ROOT): rendering.register_menu_commands("en"),
        (CHAT_ID, ADMIN): rendering.register_menu_commands("en"),
    }
    assert len(transport.menu_calls) == 3


async def test_private_chat_remove_deletes_the_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await handle_event(
        ctx, private_event(f"/chat remove {CHAT_ID}", update_id=1, user_id=ROOT)
    )
    assert _chat_ops(transport) == [("delete", CHAT_ID)]


async def test_admin_revoke_cascade_deletes_every_dropped_chat(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, 501, ADMIN)
    await register_chat(db, services, 502, ADMIN)
    await handle_event(
        ctx, private_event(f"/admin remove {ADMIN}", update_id=1, user_id=ROOT)
    )
    assert sorted(_chat_ops(transport)) == [("delete", 501), ("delete", 502)]
    # the revoked admin loses the member menu, root (still staff) may register again
    assert _member_ops(transport) == {
        (501, ADMIN): None, (502, ADMIN): None,
        (501, ROOT): rendering.register_menu_commands("en"),
        (502, ROOT): rendering.register_menu_commands("en"),
    }


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
    assert _member_ops(transport) == {}  # the bot is gone: nothing more to reach


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
    got = sorted(_chat_ops(transport))
    assert got == sorted([("delete", CHAT_ID), ("set", new)])
    assert set(_member_ops(transport)) == {(new, ROOT), (new, ADMIN)}


async def test_migration_found_by_a_failed_reply_syncs_both_ids(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.raise_next(ChatMigrated(-1001))
    await _say(ctx, "/upb list", 1, ADMIN)
    got = sorted(_chat_ops(transport))
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
    transport.fail_menu(RateLimited(4.0))
    assert await ctx.delivery.sync_chat_menu(CHAT_ID) is False
    assert len(transport.menu_calls) == 3
    assert slept == [4.0, 4.0]


async def test_429_longer_than_the_wait_cap_gives_up_without_waiting(db):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.fail_menu(RateLimited(MENU_MAX_WAIT + 1))
    assert await ctx.delivery.sync_chat_menu(CHAT_ID) is False
    assert len(transport.menu_calls) == 1 and slept == []
    transport.fail_menu(None)
    transport.queue_menu_raises([RateLimited(MENU_MAX_WAIT)])  # exactly the cap is waited out
    assert await ctx.delivery.sync_chat_menu(CHAT_ID) is True
    assert slept == [MENU_MAX_WAIT]


@pytest.mark.parametrize("text", ["/upb chat register", "/upb lang ru", "/upb chat unregister"])
async def test_command_triggered_menu_sync_makes_one_attempt_and_never_waits(db, text):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    if text != "/upb chat register":
        await register_chat(db, services, CHAT_ID, ADMIN)
    transport.fail_menu(RateLimited(1.0))
    await _say(ctx, text, 1, ADMIN)
    assert len(transport.menu_calls) == 1
    assert slept == []


async def test_startup_sync_stops_the_batch_on_a_429_longer_than_the_cap(db):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    for chat in (-1, -2, -3):
        await register_chat(db, services, chat, ADMIN)
    transport.fail_menu(RateLimited(MENU_MAX_WAIT + 1))
    await entry.sync_menus(ctx, [])  # does not raise
    assert len(transport.menu_calls) == 1 and slept == []


async def test_a_long_429_stops_the_rest_of_the_chats_calls_at_startup(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, -1, ADMIN)
    await register_chat(db, services, -2, ADMIN)
    # chat scope ok, then the first member call floods
    calls = []
    real = transport.set_chat_commands

    async def flaky(chat_id, commands, *, user_id=None):
        calls.append((chat_id, user_id))
        if user_id is not None:
            raise RateLimited(MENU_MAX_WAIT + 1)
        await real(chat_id, commands, user_id=user_id)

    transport.set_chat_commands = flaky
    await entry.sync_menus(ctx, [])
    assert calls == [(-2, None), (-2, ROOT)]  # ascending ids; chat -1 is not reached


async def test_outbox_sync_ignores_the_startup_abort(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    for chat in (-1, -2, -3):
        await register_chat(db, services, chat, ADMIN)
    transport.fail_menu(RateLimited(MENU_MAX_WAIT + 1))
    await ctx.delivery.sync_chat_menus([-1, -2, -3])  # as from the outbox
    assert {c["chat_id"] for c in transport.menu_calls} == {-1, -2, -3}


async def test_a_nested_sync_does_not_clear_the_startup_abort(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, -1, ADMIN)
    await register_chat(db, services, -2, ADMIN)
    transport.fail_menu(RateLimited(MENU_MAX_WAIT + 1))
    await ctx.delivery.sync_chat_menu(-9, single_attempt=True)  # another task's sync
    transport.menu_calls.clear()
    await entry.sync_menus(ctx, [])
    assert len(transport.menu_calls) == 1


async def test_startup_sync_goes_on_after_a_429_within_the_cap_was_waited_out(db):
    clock = FakeClock()

    async def sleep(seconds):
        pass

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    for chat in (-1, -2):
        await register_chat(db, services, chat, ADMIN)
    transport.queue_menu_raises([RateLimited(1.0)])
    await entry.sync_menus(ctx, [])
    assert {c["chat_id"] for c in transport.menu_calls} == {-1, -2}


async def test_startup_sync_picks_chats_from_the_outbox(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, -1, ADMIN)
    async with db.transaction() as c:
        await services.unregister_chat(c, -1)  # queues a farewell, no registration left
    await entry.sync_menus(ctx, [])
    ops = [(m["chat_id"], m["user_id"], m["op"]) for m in transport.menu_calls]
    assert ops == [(-1, None, "delete"), (-1, ROOT, "set"), (-1, ADMIN, "set")]  # staff get /register


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
    got = _chat_ops(transport)
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
    got = _chat_ops(transport)
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


async def test_unauthorized_from_a_member_scope_call_propagates(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    real = transport.set_chat_commands

    async def set_commands(chat_id, commands, *, user_id=None):
        if user_id is not None:
            raise Unauthorized()
        await real(chat_id, commands, user_id=user_id)

    transport.set_chat_commands = set_commands
    with pytest.raises(Unauthorized):
        await handle_event(ctx, group_event("/register", update_id=1, user_id=ADMIN, chat_id=CHAT_ID))


# --- unregister reaches all staff; a revoked admin is purged from known chats ---

OTHER = 2  # a second admin, not an owner of the chat


async def _two_admins_chat(db, services):
    await _world(db, services)
    await make_admin(db, services, OTHER)
    await register_chat(db, services, CHAT_ID, ADMIN)


def _register_menus(transport):
    reg = rendering.register_menu_commands("en")
    assert _member_ops(transport) == {(CHAT_ID, u): reg for u in (ROOT, ADMIN, OTHER)}
    assert len(transport.menu_calls) == 4  # chat scope and three members


async def test_group_unregister_gives_every_staff_member_the_register_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _two_admins_chat(db, services)
    await _say(ctx, "/unregister", 1, ADMIN)
    _register_menus(transport)


async def test_private_chat_remove_gives_every_staff_member_the_register_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _two_admins_chat(db, services)
    await handle_event(ctx, private_event(f"/chat remove {CHAT_ID}", update_id=1, user_id=ROOT))
    _register_menus(transport)


async def test_farewell_sync_gives_every_staff_member_the_register_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _two_admins_chat(db, services)
    async with db.transaction() as c:  # leaves a farewell row; its sync runs from the outbox
        await services.unregister_chat(c, CHAT_ID)
    await ctx.delivery.run_outbox_once()
    _register_menus(transport)


async def test_unregister_deletes_the_member_menu_of_a_non_staff_owner(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _two_admins_chat(db, services)
    async with db.transaction() as c:
        await services.revoke_admin(c, OTHER)
        await services.grant_admin(c, OTHER)
    await register_chat(db, services, 501, 3)  # registrar 3 has no role
    await _say(ctx, "/unregister", 1, ROOT, chat_id=501)
    assert _member_ops(transport)[(501, 3)] is None
    assert _member_ops(transport)[(501, OTHER)] == rendering.register_menu_commands("en")


async def _known_chats(db, services):
    """Active 501 (registered by root), inactive 502 (unregistered: a farewell row remains)."""
    await _world(db, services)
    await register_chat(db, services, 501, ROOT)
    await register_chat(db, services, 502, ROOT)
    async with db.transaction() as c:
        await services.unregister_chat(c, 502)


async def _record(db, services, chat_id, user_id, kind="register"):
    async with db.transaction() as c:
        await services.record_member_menu(c, chat_id, user_id, kind)


async def _recorded(db, services, user_id):
    async with db.reader() as c:
        return await services.member_menu_chats(c, user_id)


async def test_admin_revoke_deletes_the_member_menu_in_the_recorded_chats_only(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _known_chats(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    # 7777: a group the bot was added to and nobody registered; 888: someone else's menu only
    for chat_id in (501, 502, 7777):
        await _record(db, services, chat_id, ADMIN)
    await _record(db, services, 888, ROOT)
    transport.menu_calls.clear()
    await handle_event(ctx, private_event(f"/admin remove {ADMIN}", update_id=1, user_id=ROOT))
    purge = [(c["chat_id"], c["op"]) for c in transport.menu_calls if c["user_id"] == ADMIN]
    assert sorted(purge) == [(CHAT_ID, "delete"), (501, "delete"), (502, "delete"), (7777, "delete")]
    # the cascade chat is handled once: no second delete for the same member
    assert len(transport.menu_calls) == 6  # 3 purges + the cascade's chat, admin and root calls
    assert all(c["chat_id"] != 888 for c in transport.menu_calls)
    assert await _recorded(db, services, ADMIN) == []
    assert await _recorded(db, services, ROOT) == [CHAT_ID, 888]  # + the register menu just set


async def test_admin_revoke_purge_makes_one_attempt_and_logs_errors(db, caplog):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _known_chats(db, services)
    for chat_id in (501, 502):
        await _record(db, services, chat_id, ADMIN)
    transport.menu_calls.clear()
    real = transport.delete_chat_commands

    async def delete(chat_id, *, user_id=None):
        await real(chat_id, user_id=user_id)
        if user_id == ADMIN:
            raise PermanentSend() if chat_id == 501 else RateLimited(1.0)

    transport.delete_chat_commands = delete
    with caplog.at_level(logging.DEBUG):
        await handle_event(ctx, private_event(f"/admin remove {ADMIN}", update_id=1, user_id=ROOT))
    assert [(c["chat_id"], c["user_id"]) for c in transport.menu_calls] == [(501, ADMIN), (502, ADMIN)]
    assert slept == []
    assert any(r.getMessage().startswith("menu_delete_permanent ") for r in caplog.records)
    assert any(r.getMessage().startswith("menu_delete_retries_exhausted ") for r in caplog.records)
    assert await _recorded(db, services, ADMIN) == [501, 502]  # failed deletes stay recorded


async def test_unauthorized_from_the_admin_revoke_purge_propagates(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _known_chats(db, services)
    await _record(db, services, 501, ADMIN)
    transport.fail_menu(Unauthorized())
    with pytest.raises(Unauthorized):
        await handle_event(ctx, private_event(f"/admin remove {ADMIN}", update_id=1, user_id=ROOT))


async def test_cli_root_change_deletes_the_old_root_menu_in_inactive_known_chats(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, CHAT_ID, ROOT)
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT_ID)
        await services.touch_user(c, 11)
        await services.set_root(c, 11)
    await ctx.delivery.run_outbox_once()
    members = _member_ops(transport)
    assert members[(CHAT_ID, ROOT)] is None
    assert members[(CHAT_ID, 11)] == rendering.register_menu_commands("en")


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
    # the farewell sync, then the root_revoked sync over the known chats
    assert _chat_ops(transport) == [("delete", CHAT_ID)] * 2
    # the old root is no longer staff, the new one may register the free chat
    assert _member_ops(transport) == {
        (CHAT_ID, ROOT): None,
        (CHAT_ID, 11): rendering.register_menu_commands("en"),
    }


@pytest.mark.parametrize("fail", [None, PermanentSend()])
async def test_cli_root_change_syncs_active_chats_once_the_notice_is_final(db, fail):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await make_admin(db, services, ADMIN)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await _record(db, services, CHAT_ID, ROOT, "owner")
    async with db.transaction() as c:  # what the CLI does: no transport
        await services.touch_user(c, 11)
        await services.set_root(c, 11)
    assert transport.menu_calls == []
    if fail is not None:
        transport.fail_chat(ROOT, fail)
    await ctx.delivery.run_outbox_once()
    members = _member_ops(transport)
    assert _chat_ops(transport) == [("set", CHAT_ID)]
    assert members[(CHAT_ID, ROOT)] is None  # the old root loses the owner menu
    assert members[(CHAT_ID, 11)] == rendering.owner_menu_commands("en")
    assert members[(CHAT_ID, ADMIN)] == rendering.owner_menu_commands("en")


async def test_retryable_root_notice_failure_does_not_sync_menus_yet(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await make_admin(db, services, ADMIN)
    await register_chat(db, services, CHAT_ID, ADMIN)
    async with db.transaction() as c:
        await services.touch_user(c, 11)
        await services.set_root(c, 11)
    transport.fail_chat(ROOT, AmbiguousSend())
    await ctx.delivery.run_outbox_once()
    assert transport.menu_calls == []


async def test_root_notice_menu_sync_waits_on_429_only_up_to_the_cap(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await make_admin(db, services, ADMIN)
    await register_chat(db, services, CHAT_ID, ADMIN)
    async with db.transaction() as c:
        await services.touch_user(c, 11)
        await services.set_root(c, 11)
    transport.fail_menu(RateLimited(MENU_MAX_WAIT + 1))
    await ctx.delivery.run_outbox_once()  # gives up on the chat scope, never raises
    assert len(transport.menu_calls) == 1


async def test_retryable_farewell_failure_does_not_touch_the_menu_yet(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, CHAT_ID, ROOT)
    async with db.transaction() as c:
        await services.unregister_chat(c, CHAT_ID)
    transport.fail_chat(CHAT_ID, AmbiguousSend())
    await ctx.delivery.run_outbox_once()
    assert transport.menu_calls == []


def _my_member(uid, *, added, user=ADMIN):
    return make_event(
        kind="my_chat_member", update_id=uid, chat_id=CHAT_ID, chat_type="supergroup",
        user_id=user, bot_added=added, text=None,
    )


async def test_bot_added_to_an_unregistered_group_deletes_a_stale_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _my_member(1, added=True))
    assert _chat_ops(transport) == [("delete", CHAT_ID)]
    assert transport.calls == []
    await handle_event(ctx, _my_member(1, added=True))  # a replayed update does nothing
    assert len(transport.menu_calls) == 3  # chat scope, root, admin


@pytest.mark.parametrize("adder", [ROOT, ADMIN])
async def test_staff_adding_the_bot_gets_the_register_menu_in_that_group(db, adder):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.set_user_lang(c, adder, "ru")
    await handle_event(ctx, _my_member(1, added=True, user=adder))
    register = rendering.register_menu_commands
    other = ADMIN if adder == ROOT else ROOT
    # every staff member is a candidate, each in their own language
    assert _member_ops(transport) == {
        (CHAT_ID, adder): register("ru"),
        (CHAT_ID, other): register("en"),
    }
    assert [n for n, _ in _member_ops(transport)[(CHAT_ID, adder)]] == REGISTER_COMMANDS
    assert _chat_ops(transport) == [("delete", CHAT_ID)]


async def test_a_non_staff_adder_gets_no_member_menu(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await handle_event(ctx, _my_member(1, added=True, user=77))
    assert _member_ops(transport) == {
        (CHAT_ID, ROOT): rendering.register_menu_commands("en"),
        (CHAT_ID, ADMIN): rendering.register_menu_commands("en"),
        (CHAT_ID, 77): None,
    }


async def test_a_former_admin_adding_the_bot_has_the_member_menu_deleted(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    async with db.transaction() as c:
        await services.revoke_admin(c, ADMIN)
    await handle_event(ctx, _my_member(1, added=True, user=ADMIN))
    assert _member_ops(transport) == {
        (CHAT_ID, ROOT): rendering.register_menu_commands("en"),
        (CHAT_ID, ADMIN): None,  # no longer staff
    }


async def test_adding_the_bot_to_a_registered_chat_syncs_the_owner_menus(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await handle_event(ctx, _my_member(1, added=True, user=ADMIN))
    assert _chat_ops(transport) == [("set", CHAT_ID)]
    assert set(_member_ops(transport)) == {(CHAT_ID, ROOT), (CHAT_ID, ADMIN)}
    assert _member_ops(transport)[(CHAT_ID, ADMIN)] == rendering.owner_menu_commands("en")


async def test_a_kicked_and_re_added_bot_resets_the_stale_owner_menus(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await make_admin(db, services, 2)
    await _say(ctx, "/register", 1, ADMIN)  # root and ADMIN hold owner menus
    await handle_event(
        ctx,
        make_event(
            kind="my_chat_member", update_id=2, chat_id=CHAT_ID, chat_type="supergroup",
            user_id=ADMIN, bot_removed=True, text=None,
        ),
    )  # kicked: no farewell
    transport.menu_calls.clear()
    await handle_event(ctx, _my_member(3, added=True, user=2))
    members = _member_ops(transport)
    register = rendering.register_menu_commands("en")
    assert members == {(CHAT_ID, ROOT): register, (CHAT_ID, ADMIN): register, (CHAT_ID, 2): register}


async def test_staff_register_menu_is_replaced_when_someone_else_registers(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await make_admin(db, services, 2)
    await handle_event(ctx, _my_member(1, added=True, user=2))  # 2 got the register menu
    assert _member_ops(transport)[(CHAT_ID, 2)] == rendering.register_menu_commands("en")
    await _say(ctx, "/register", 2, ROOT)
    members = _member_ops(transport)
    assert members[(CHAT_ID, ROOT)] == rendering.owner_menu_commands("en")
    assert members[(CHAT_ID, ADMIN)] is None  # a member scope would hide the chat menu
    assert members[(CHAT_ID, 2)] is None


async def test_adder_menu_is_replaced_when_the_bot_is_re_added_to_a_registered_chat(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await make_admin(db, services, 2)
    await register_chat(db, services, CHAT_ID, ADMIN)
    await handle_event(ctx, _my_member(1, added=True, user=2))  # 2 is staff, not an owner here
    assert _member_ops(transport)[(CHAT_ID, 2)] is None


async def test_admin_revoke_of_a_registrar_reaches_the_member_scopes_once(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await _say(ctx, "/register", 1, ADMIN)
    transport.menu_calls.clear()
    await handle_event(ctx, private_event(f"/admin remove {ADMIN}", update_id=2, user_id=ROOT))
    ops = [(c["op"], c["chat_id"], c["user_id"]) for c in transport.menu_calls]
    assert sorted(ops, key=str) == sorted(
        [("delete", CHAT_ID, None), ("delete", CHAT_ID, ADMIN), ("set", CHAT_ID, ROOT)], key=str
    )


async def test_member_scope_sync_makes_one_attempt_per_call_on_command_paths(db):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    await handle_event(ctx, _my_member(1, added=True))
    transport.menu_calls.clear()
    real = transport.set_chat_commands

    async def set_commands(chat_id, commands, *, user_id=None):
        await real(chat_id, commands, user_id=user_id)
        if user_id is not None:
            raise RateLimited(1.0)

    transport.set_chat_commands = set_commands
    await handle_event(ctx, _my_member(2, added=True, user=ROOT))
    assert [c["user_id"] for c in transport.menu_calls] == [None, ROOT, ADMIN]  # one attempt each
    assert slept == []

    transport.menu_calls.clear()
    transport.set_chat_commands = real
    transport.fail_menu(RateLimited(1.0))
    await handle_event(ctx, _my_member(3, added=True, user=ROOT))
    assert len(transport.menu_calls) == 1  # the chat scope is out of reach: members are skipped
    assert slept == []


async def test_join_triggered_delete_makes_one_attempt_and_never_waits_on_429(db):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    transport.fail_menu(RateLimited(1.0))  # within the cap: only single_attempt stops a retry
    await handle_event(ctx, _my_member(1, added=True))
    assert len(transport.menu_calls) == 1  # one attempt, no wait
    assert slept == []


def _event_remove_chat(uid):
    return private_event(f"/chat remove {CHAT_ID}", update_id=uid, user_id=ROOT)


def _event_remove_admin(uid):
    return private_event(f"/admin remove {ADMIN}", update_id=uid, user_id=ROOT)


def _event_bot_kicked(uid):
    return make_event(
        kind="my_chat_member", update_id=uid, chat_id=CHAT_ID, chat_type="supergroup",
        user_id=ADMIN, bot_removed=True, text=None,
    )


def _event_migrate_to(uid):
    return make_event(
        kind="message", update_id=uid, chat_id=CHAT_ID, user_id=None, text=None,
        migrate_to_chat_id=-1001,
    )


def _event_migrate_from(uid):
    return make_event(
        kind="message", update_id=uid, chat_id=-1001, user_id=None, text=None,
        migrate_from_chat_id=CHAT_ID,
    )


@pytest.mark.parametrize(
    "make,calls",
    [
        (_event_remove_chat, 1),
        (_event_remove_admin, 1),
        (_event_bot_kicked, 1),
        (_event_migrate_to, 2),  # one attempt for each of the two ids
        (_event_migrate_from, 2),
    ],
)
async def test_event_paths_make_one_menu_attempt_per_chat_and_never_wait(db, make, calls):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.fail_menu(RateLimited(1.0))
    await handle_event(ctx, make(1))
    assert len(transport.menu_calls) == calls
    assert slept == []


async def test_migration_found_by_a_failed_reply_makes_one_menu_attempt_per_id(db):
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock.sleep = sleep
    ctx, services, transport, _c = mk_ctx(db, clock)
    await _world(db, services)
    await register_chat(db, services, CHAT_ID, ADMIN)
    transport.fail_menu(RateLimited(1.0))
    transport.raise_next(ChatMigrated(-1001))
    await _say(ctx, "/upb list", 1, ADMIN)
    assert len(transport.menu_calls) == 2 and slept == []


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
    got = {c["chat_id"]: c["op"] for c in transport.menu_calls if c["user_id"] is None}
    assert got == {-1: "set", -200: "set", -9: "delete"}  # the migrated-away id is skipped
    members = _member_ops(transport)
    assert {k: v for k, v in members.items() if k[0] in (-1, -200)} == {
        (chat, u): rendering.owner_menu_commands("en") for chat in (-1, -200) for u in (ROOT, ADMIN)
    }
    assert members[(-9, ROOT)] == rendering.register_menu_commands("en")  # inactive: staff get /register


async def test_startup_sync_takes_a_former_root_from_the_outbox(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await make_admin(db, services, ADMIN)
    await register_chat(db, services, -1, ADMIN)
    async with db.transaction() as c:  # the CLI: no transport, no menu calls
        await services.touch_user(c, 11)
        await services.set_root(c, 11)
    await entry.sync_menus(ctx, [])
    members = _member_ops(transport)
    assert members[(-1, ROOT)] is None  # demoted: the owner menu goes
    assert members[(-1, 11)] == rendering.owner_menu_commands("en")  # the new root gets it
    assert members[(-1, ADMIN)] == rendering.owner_menu_commands("en")


async def test_startup_sync_does_not_duplicate_a_registrar_who_was_root(db):
    ctx, services, transport, _c = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, -1, ROOT)
    async with db.transaction() as c:
        await services.touch_user(c, 11)
        await services.set_root(c, 11)  # drops the chat registered by the old root
    async with db.transaction() as c:
        await services.set_root(c, ROOT)  # and back
    await register_chat(db, services, -2, ROOT)
    await entry.sync_menus(ctx, [])
    ops = [(c["chat_id"], c["user_id"], c["op"]) for c in transport.menu_calls if c["chat_id"] == -2]
    assert ops.count((-2, ROOT, "set")) == 1


async def test_startup_sync_gives_owners_their_menu_in_the_chat_language(db):
    ctx, services, transport, _c = mk_ctx(db)
    await _world(db, services)
    await register_chat(db, services, -1, ADMIN)
    async with db.transaction() as c:
        await services.set_chat_lang(c, -1, "ru")
    await entry.sync_menus(ctx, [])
    assert _member_ops(transport)[(-1, ADMIN)] == rendering.owner_menu_commands("ru")


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
    got = {c["chat_id"]: c["op"] for c in transport.menu_calls if c["user_id"] is None}
    assert got == {-1: "set", -2: "delete"}


async def test_signal_during_the_startup_menu_sync_still_sends_the_report(db):
    _ctx, services, transport, clock = mk_ctx(db)
    await make_root(db, services, ROOT, contact=True)
    await register_chat(db, services, -1, ROOT)
    await register_chat(db, services, -2, ROOT)
    transport.set_probe(-2, PermanentSend())
    stop = asyncio.Event()
    real_set = transport.set_chat_commands

    async def set_and_signal(chat_id, commands, *, user_id=None):
        stop.set()  # a stop arrives while the menus are being synced
        await real_set(chat_id, commands, user_id=user_id)

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


async def test_real_transport_member_scope(bot_and_session):
    transport, session = bot_and_session
    session.replies += [(200, {"ok": True, "result": True})] * 2
    await transport.set_chat_commands(CHAT, [("register", "Register")], user_id=77)
    await transport.delete_chat_commands(CHAT, user_id=77)
    (_n1, p1), (_n2, p2) = session.requests
    assert p1["scope"] == {"type": "chat_member", "chat_id": CHAT, "user_id": 77}
    assert p2["scope"] == {"type": "chat_member", "chat_id": CHAT, "user_id": 77}


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
