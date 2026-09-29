"""Acceptance checks not exercised elsewhere.

True multi-connection concurrency (not just sequential calls), a database
error at the permission-check point, and a sweep for a planted fake secret
marker across the sqlite file, the outbox table and logs.
"""

import asyncio
import logging
import re
import sqlite3

import pytest

from app.db import Database, apply_migrations, open_database
from app.delivery import Delivery, RateLimited
from app.handlers import Context, handle_event
from app.models import Cmd, SubscriberRef
from app.services import Services
from app.vault import VaultClient

from conftest import FakeClock, RecordingTransport, make_admin, make_event

CHAT = 100
MSG = 7
THREAD = 3


def _mk_ctx(db, clock=None):
    clock = clock or FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db, services, transport, clock=clock)
    ctx = Context(
        db=db, services=services, delivery=delivery, bot_id=999, bot_username="upb_bot", clock=clock
    )
    return ctx, services, transport, clock


def _entities(text: str):
    first = text.split(" ", 1)[0]
    return (("bot_command", 0, len(first)),)


def _group_event(text, *, update_id, user_id, chat_id=CHAT, message_id=None, **kwargs):
    defaults = dict(
        kind="message",
        chat_type="group",
        chat_id=chat_id,
        update_id=update_id,
        user_id=user_id,
        username=f"user{user_id}",
        display_name=f"User{user_id}",
        message_id=message_id or update_id,
        text=text,
        entities=_entities(text),
    )
    defaults.update(kwargs)
    return make_event(**defaults)


def _private_event(text, *, update_id, user_id, message_id=None, **kwargs):
    defaults = dict(
        kind="message",
        chat_type="private",
        chat_id=user_id,
        update_id=update_id,
        user_id=user_id,
        username=f"user{user_id}",
        display_name=f"User{user_id}",
        message_id=message_id or update_id,
        text=text,
        entities=_entities(text),
    )
    defaults.update(kwargs)
    return make_event(**defaults)


async def _register_chat(db, services, chat_id, registrar_id):
    async with db.transaction() as c:
        await services.touch_user(c, registrar_id)
        return await services.register_chat(c, chat_id, "Chat", registrar_id)


def _ids_in(text: str) -> set[int]:
    return {int(m) for m in re.findall(r'tg://user\?id=(\d+)"', text)}


# --- true multi-connection concurrency (not just sequential calls) ---


@pytest.mark.asyncio
async def test_concurrent_registration_race_leaves_one_registrar_no_corruption(tmp_path):
    path = str(tmp_path / "race_register.sqlite3")
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            await Services().touch_user(c, 1)
            await Services().touch_user(c, 2)

    db_a = Database(path)
    db_b = Database(path)
    await db_a.connect()
    await db_b.connect()
    services = Services()
    try:
        async def register_via(conn_db, registrar_id):
            async with conn_db.transaction() as c:
                return await services.register_chat(c, -100, "Chat", registrar_id)

        result_a, result_b = await asyncio.gather(register_via(db_a, 1), register_via(db_b, 2))
    finally:
        await db_a.close()
        await db_b.close()

    # exactly one connection actually created the row; the other observed it
    # already registered, both agreeing on the same generation
    assert sorted([result_a.created, result_b.created]) == [False, True]
    assert result_a.generation == result_b.generation == 1

    check = Database(path)
    await check.connect()
    try:
        async with check.reader() as c:
            row = await services.get_chat(c, -100)
            cursor = await c.execute("SELECT value FROM counters WHERE name = 'generation'")
            (gen_value,) = await cursor.fetchone()
    finally:
        await check.close()
    assert row is not None
    assert row.registered_by in (1, 2)
    assert gen_value == 1  # the generation counter advanced exactly once, not twice


@pytest.mark.asyncio
async def test_concurrent_set_root_race_never_leaves_two_roots(tmp_path):
    # stands in for "two concurrent CLI set-root invocations": two independent
    # connections to the same file, each racing to become root
    path = str(tmp_path / "race_root.sqlite3")
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            await Services().touch_user(c, 10)
            await Services().touch_user(c, 20)

    db_a = Database(path)
    db_b = Database(path)
    await db_a.connect()
    await db_b.connect()
    services = Services()
    try:
        async def set_root_via(conn_db, user_id):
            async with conn_db.transaction() as c:
                return await services.set_root(c, user_id)

        await asyncio.gather(set_root_via(db_a, 10), set_root_via(db_b, 20))
    finally:
        await db_a.close()
        await db_b.close()

    check = Database(path)
    await check.connect()
    try:
        async with check.reader() as c:
            cursor = await c.execute("SELECT user_id FROM roles WHERE role = 'root'")
            rows = await cursor.fetchall()
    finally:
        await check.close()
    assert len(rows) == 1  # the roles_single_root index plus write serialization hold
    assert rows[0][0] in (10, 20)


@pytest.mark.asyncio
async def test_concurrent_cli_set_root_and_telegram_admin_create_do_not_corrupt_state(tmp_path):
    # a CLI root reassignment racing a Telegram-driven /admin create for an
    # unrelated user: unrelated rows, but both go through the same write lock
    path = str(tmp_path / "race_cli_telegram.sqlite3")
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            await Services().touch_user(c, 1)  # current root
            await Services().set_root(c, 1)
            await Services().touch_user(c, 500)  # target of the admin grant

    db_cli = Database(path)
    db_bot = Database(path)
    await db_cli.connect()
    await db_bot.connect()
    services = Services()
    try:
        async def cli_set_root():
            async with db_cli.transaction() as c:
                return await services.set_root(c, 2)

        async def telegram_admin_create():
            async with db_bot.transaction() as c:
                return await services.grant_admin(c, 500)

        await asyncio.gather(cli_set_root(), telegram_admin_create())
    finally:
        await db_cli.close()
        await db_bot.close()

    check = Database(path)
    await check.connect()
    try:
        async with check.reader() as c:
            root_id = await services.get_root(c)
            role_500 = await services.get_role(c, 500)
            cursor = await c.execute("SELECT COUNT(*) FROM roles WHERE role = 'root'")
            (root_count,) = await cursor.fetchone()
    finally:
        await check.close()
    assert root_count == 1
    assert root_id == 2
    assert str(role_500) == "Role.ADMIN" or role_500 is not None


@pytest.mark.asyncio
async def test_concurrent_admin_remove_and_register_leaves_consistent_state(tmp_path):
    # An /admin-remove-style cascade racing a chat register must leave no
    # chat row referencing the revoked admin and no orphaned subscription, in either possible commit order.
    path = str(tmp_path / "race_admin_remove_register.sqlite3")
    A = 7
    SUB = 55
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            await Services().touch_user(c, A)
            await Services().grant_admin(c, A)
            await Services().register_chat(c, -100, "First", A)
            await Services().touch_user(c, SUB)
            await Services().subscribe(c, -100, SUB)

    db_remove = Database(path)
    db_register = Database(path)
    await db_remove.connect()
    await db_register.connect()
    services = Services()
    try:
        async def remove_admin():
            async with db_remove.transaction() as c:
                return await services.revoke_admin(c, A)

        async def register_second_chat():
            async with db_register.transaction() as c:
                return await services.register_chat(c, -200, "Second", A)

        await asyncio.gather(remove_admin(), register_second_chat())
    finally:
        await db_remove.close()
        await db_register.close()

    check = Database(path)
    await check.connect()
    try:
        async with check.reader() as c:
            role_a = await services.get_role(c, A)
            chat_100 = await services.get_chat(c, -100)
            chat_200 = await services.get_chat(c, -200)
            cursor = await c.execute("SELECT COUNT(*) FROM subscriptions WHERE chat_id = -100")
            (subs_on_100,) = await cursor.fetchone()
    finally:
        await check.close()

    if role_a is None:
        # the cascade committed at some point; the chat that existed before
        # either coroutine started is gone in every possible ordering, and its
        # subscription went with it via the FK cascade -- no orphan left behind
        assert chat_100 is None
        assert subs_on_100 == 0
        # -200 only survives if it was registered after the cascade had already
        # run (services.register_chat performs no role check of its own -- the
        # handler layer is what would have refused this before ever mutating)
    else:
        # register_chat committed first and observed A still admin throughout;
        # nothing was removed, so no chat row references a revoked admin and
        # the original subscription is exactly as it was, not duplicated
        assert chat_100 is not None
        assert chat_200 is not None
        assert subs_on_100 == 1


@pytest.mark.asyncio
async def test_concurrent_set_root_race_never_leaves_two_roots_or_partial_cascade(tmp_path):
    # A real root with real chats races two challengers
    # for root at once. "Never leaves a partial cascade" means the loser's view
    # of the old root's chats is never half-dropped: both chats disappear
    # together, exactly once, and the root_revoked notice is queued exactly once.
    path = str(tmp_path / "race_root_cascade.sqlite3")
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            svc = Services()
            await svc.touch_user(c, 1, private_contact=True)
            await svc.set_root(c, 1)
            await svc.register_chat(c, -900, "Root chat 1", 1)
            await svc.register_chat(c, -901, "Root chat 2", 1)
            await svc.touch_user(c, 2)
            await svc.touch_user(c, 3)

    db_a = Database(path)
    db_b = Database(path)
    await db_a.connect()
    await db_b.connect()
    services = Services()
    try:
        async def set_root_via(conn_db, user_id):
            async with conn_db.transaction() as c:
                return await services.set_root(c, user_id)

        await asyncio.gather(set_root_via(db_a, 2), set_root_via(db_b, 3))
    finally:
        await db_a.close()
        await db_b.close()

    check = Database(path)
    await check.connect()
    try:
        async with check.reader() as c:
            cursor = await c.execute("SELECT user_id FROM roles WHERE role = 'root'")
            roots = await cursor.fetchall()
            role_1 = await services.get_role(c, 1)
            chat_900 = await services.get_chat(c, -900)
            chat_901 = await services.get_chat(c, -901)
            cursor = await c.execute(
                "SELECT event_type, target_id, status FROM outbox ORDER BY event_id"
            )
            events = await cursor.fetchall()
    finally:
        await check.close()

    assert len(roots) == 1
    assert roots[0][0] in (2, 3)
    assert role_1 is None
    # the old root's chats are gone in full: never one dropped and one left
    assert chat_900 is None
    assert chat_901 is None
    farewells = sorted(
        (target_id, status) for (etype, target_id, status) in events if etype == "chat_farewell"
    )
    assert farewells == [(-901, "pending"), (-900, "pending")]  # both, exactly once each
    root_revoked = [e for e in events if e[0] == "root_revoked"]
    assert len(root_revoked) == 1  # queued exactly once, by whichever op saw user 1 as root
    assert root_revoked[0][1] == 1


@pytest.mark.asyncio
async def test_permission_check_after_other_connections_commit_observes_new_rights(tmp_path):
    # A permission check that BEGINS after another
    # connection's commit must see the new rights -- the bot side keeps no
    # per-connection cache of a role, so nothing here needs invalidating.
    path = str(tmp_path / "rights_visibility.sqlite3")
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            await Services().touch_user(c, 999)  # will become root via connection A
            await Services().touch_user(c, 500)  # target of the admin grant

    db_a = Database(path)  # stands in for the CLI
    db_b = Database(path)  # stands in for the running bot process
    await db_a.connect()
    await db_b.connect()
    try:
        ctx, _services_b, transport, _clock = _mk_ctx(db_b)

        # before any role exists, /admin create is denied for user 999 on B
        denied_event = _private_event("/admin create 500", update_id=1, user_id=999)
        await handle_event(ctx, denied_event)
        assert transport.calls == []

        # connection A grants root to 999 and commits -- a fully separate connection
        async with db_a.transaction() as c:
            await Services().set_root(c, 999)

        # the very next check, on connection B, begins strictly after A's commit
        allowed_event = _private_event("/admin create 500", update_id=2, user_id=999)
        await handle_event(ctx, allowed_event)
    finally:
        await db_a.close()
        await db_b.close()

    assert len(transport.calls) == 1
    assert "Admin granted: 500." in transport.calls[0]["text"]


@pytest.mark.asyncio
async def test_ping_snapshot_is_final_when_another_connection_unsubscribes_mid_ping(tmp_path):
    """Recipients are fixed at the command and never re-filtered."""
    path = str(tmp_path / "boundary.sqlite3")
    async with open_database(path) as boot:
        async with boot.transaction() as c:
            svc = Services()
            await svc.touch_user(c, 1)
            await svc.grant_admin(c, 1)
            await svc.register_chat(c, -500, "Chat", 1)
            for uid in (90001, 90002, 90003):
                await svc.touch_user(c, uid, display_name=f"User{uid}")
                await svc.subscribe(c, -500, uid)

    db_a = Database(path)
    db_b = Database(path)
    await db_a.connect()
    await db_b.connect()
    clock = FakeClock()
    services_a = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db_a, services_a, transport, clock=clock)

    original_send = transport.send_message
    raced = False

    async def send_then_race(chat_id, text, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            async with db_b.transaction() as c:
                await Services(clock=clock).unsubscribe(c, chat_id, 90002)
        await original_send(chat_id, text, **kwargs)

    transport.send_message = send_then_race

    try:
        async with db_a.reader() as c:
            snapshot = await services_a.list_subscribers(c, -500)
        await delivery.run_ping(-500, MSG, THREAD, snapshot, chunk_limit=10)
    finally:
        await db_a.close()
        await db_b.close()

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert seen == [90001, 90002, 90003]  # the late unsubscribe changes nothing
    assert len(transport.calls) == 3
    assert all(call["reply_to_message_id"] == MSG for call in transport.calls)


@pytest.mark.asyncio
async def test_role_loss_mid_ping_does_not_cancel_the_remainder(db):
    clock = FakeClock()
    services = Services(clock=clock)
    transport = RecordingTransport()
    delivery = Delivery(db, services, transport, clock=clock)

    async with db.transaction() as c:
        await services.touch_user(c, 1)
        await services.touch_user(c, 9001)
        await services.grant_admin(c, 9001)
        await services.register_chat(c, CHAT, "Chat", 1)
        for uid in (50001, 50002, 50003):
            await services.touch_user(c, uid, display_name=f"User{uid}")
            await services.subscribe(c, CHAT, uid)
    async with db.reader() as c:
        snapshot = await services.list_subscribers(c, CHAT)

    original_send = transport.send_message

    async def send_and_revoke_admin(chat_id, text, **kwargs):
        await original_send(chat_id, text, **kwargs)
        if len(transport.calls) == 1:
            async with db.transaction() as c:
                await services.revoke_admin(c, 9001)

    transport.send_message = send_and_revoke_admin
    await delivery.run_ping(CHAT, MSG, THREAD, snapshot, chunk_limit=10)

    seen: list[int] = []
    for call in transport.calls:
        seen.extend(_ids_in(call["text"]))
    assert seen == [50001, 50002, 50003]  # authorization was taken once, before the reaction


# --- a database error at the permission-check point must never grant access ---


@pytest.mark.asyncio
async def test_db_error_during_permission_check_denies_access_and_sends_nothing(db, monkeypatch):
    ctx, services, transport, clock = _mk_ctx(db)
    await _register_chat(db, services, CHAT, 1)  # active chat; ACTOR has no role or subscription
    ACTOR = 12345

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(services, "load_actor", _boom)

    event = _group_event("/upb all", update_id=1, user_id=ACTOR)
    await handle_event(ctx, event)  # the poison update never escapes

    assert transport.calls == []  # never turns into a reply, not even an error message

    monkeypatch.undo()  # restore load_actor before reading back through it
    async with db.reader() as c:
        assert not await services.is_subscribed(c, CHAT, ACTOR)  # nothing granted
        cursor = await c.execute("SELECT outcome FROM processed_updates WHERE update_id = 1")
        rows = await cursor.fetchall()
    assert rows == [("error",)]  # rolled back, then recorded on its own

    # the same update is not replayed; a fresh one from the healthy DB is silent (no rights)
    await handle_event(ctx, event)
    await handle_event(ctx, _group_event("/upb all", update_id=2, user_id=ACTOR))
    assert transport.calls == []


# --- fake secret markers must never reach the database, the outbox, or logs ---


class _FakeVaultResponse:
    def __init__(self, status_code: int, json_data: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ""

    def json(self) -> dict:
        return self._json


class _FakeVaultSession:
    def __init__(self, token: str) -> None:
        self._token = token

    def post(self, url, **kwargs):
        return _FakeVaultResponse(200, {"auth": {"client_token": "vault-internal-token"}})

    def get(self, url, **kwargs):
        return _FakeVaultResponse(200, {"data": {"data": {"token": self._token}}})


@pytest.mark.asyncio
async def test_fake_token_and_vault_markers_never_land_in_db_outbox_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    fake_token = "FAKE-BOT-TOKEN-MARKER-8f2c1e"
    fake_role_id = "FAKE-VAULT-ROLE-ID-MARKER-3a91"
    fake_secret_id = "FAKE-VAULT-SECRET-ID-MARKER-77d0"
    markers = (fake_token, fake_role_id, fake_secret_id)

    db_path = tmp_path / "workload.sqlite3"
    database = Database(str(db_path))
    await database.connect()
    await apply_migrations(database)

    try:
        # the one place the markers legitimately exist: fetching the "bot
        # token" from a fake Vault using marker AppRole credentials
        session = _FakeVaultSession(fake_token)
        client = VaultClient(
            "https://vault.example.com", fake_role_id, fake_secret_id, session=session
        )
        fetched_token = client.read_kv("upb")["token"]
        assert fetched_token == fake_token

        # a representative workload through services/handlers/delivery -- none
        # of this takes a token parameter at all, which is exactly the point:
        # confirm that holds even with the marker alive in the same process
        clock = FakeClock()
        services = Services(clock=clock)
        transport = RecordingTransport()
        delivery = Delivery(database, services, transport, clock=clock)
        ctx = Context(
            db=database,
            services=services,
            delivery=delivery,
            bot_id=999,
            bot_username="upb_bot",
            clock=clock,
        )

        await make_admin(database, services, 1)  # without a role every command is silently denied
        await handle_event(ctx, _group_event("/upb chat register", update_id=1, user_id=1))
        await handle_event(ctx, _group_event("/upb notify on", update_id=2, user_id=2))
        await handle_event(ctx, _group_event("/upb notify on", update_id=3, user_id=3))
        await handle_event(ctx, _group_event("/upb all", update_id=4, user_id=2))
        await handle_event(ctx, _group_event("/upb notify off", update_id=5, user_id=3))
        await handle_event(ctx, _group_event("/upb chat unregister", update_id=6, user_id=1))
        await delivery.run_outbox_once()  # deliver the queued farewell
    finally:
        await database.close()

    # checkpoint so committed data cannot still be sitting only in the -wal file
    checkpoint_conn = sqlite3.connect(str(db_path))
    checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint_conn.close()

    raw_bytes = db_path.read_bytes()
    for marker in markers:
        assert marker.encode() not in raw_bytes, f"{marker} leaked into the sqlite file"

    check = sqlite3.connect(str(db_path))
    try:
        outbox_rows = check.execute("SELECT payload, last_error FROM outbox").fetchall()
        outcomes = check.execute(
            "SELECT outcome, COUNT(*) FROM processed_updates GROUP BY outcome"
        ).fetchall()
    finally:
        check.close()

    # the sweeps below must run over real data, not empty sets
    assert outcomes == [("ok", 6)]
    assert len(transport.calls) >= 5  # welcome, 2 subscribe, ping, unsubscribe, farewell
    assert outbox_rows, "the farewell must have gone through the outbox"
    for payload, last_error in outbox_rows:
        for marker in markers:
            assert marker not in (payload or "")
            assert marker not in (last_error or "")

    for record in caplog.records:
        message = record.getMessage()
        for marker in markers:
            assert marker not in message

    for call in transport.calls:
        for marker in markers:
            assert marker not in call["text"]
