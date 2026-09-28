import builtins
import os
import re
import socket as socket_module
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.clock import Clock
from app.db import Database, apply_migrations
from app.models import IncomingEvent

# Plan section 15 requires that the whole suite runs against temp databases and
# fake transports only: no production .env, no network sockets. The two guards
# below fail loudly (rather than silently letting a stray real access through)
# so a future test cannot accidentally reintroduce either dependency.

# Env vars a real production .env could set (see .env.example). No test may rely
# on these being present via plain os.environ fallback: every test either passes
# an explicit env mapping to config.load_config()/load_db_path(), or
# monkeypatch.setenv()s its own value.
_PRODUCTION_ENV_VARS = (
    "VAULT_ADDR",
    "VAULT_ROLE_ID",
    "VAULT_SECRET_ID",
    "VAULT_SECRET_PATH",
    "VAULT_AUTH_MOUNT",
    "VAULT_KV_MOUNT",
    "VAULT_CA_PATH",
    "VAULT_TIMEOUT",
    "UPB_DB_PATH",
    "UPB_LOG_LEVEL",
)

_REAL_ENV_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, ".env"))
_ORIGINAL_OPEN = builtins.open


def _guarded_open(file, *args, **kwargs):
    if isinstance(file, (str, bytes, os.PathLike)):
        if os.path.abspath(os.fspath(file)) == _REAL_ENV_FILE:
            raise AssertionError("tests must never read the repository's real .env file")
    return _ORIGINAL_OPEN(file, *args, **kwargs)


def _guarded_connect(self, address):
    # blocks only actual outbound connection attempts, never plain socket
    # construction: asyncio's own event loop wiring (e.g. its self-pipe) uses
    # a local AF_UNIX socketpair() internally (no connect() call) and keeps working
    raise AssertionError(f"tests must never open a real network connection to {address!r}")


@pytest.fixture(autouse=True)
def _no_network_no_production_env(monkeypatch):
    monkeypatch.setattr(builtins, "open", _guarded_open)
    monkeypatch.setattr(socket_module.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket_module.socket, "connect_ex", _guarded_connect)
    for name in _PRODUCTION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


# Telegram HTML subset: only these tags, `a` carries href only, and &, <, > outside
# tags must be entities. Unknown tags and stray angle brackets get rejected by Telegram.
ALLOWED_TAGS = frozenset(
    "b strong i em u ins s strike del a code pre tg-spoiler span blockquote".split()
)
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s+[A-Za-z-]+=\"[^\"<>]*\")*)\s*>")
_ATTR_RE = re.compile(r"\s+([A-Za-z-]+)=\"([^\"]*)\"")
_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|quot|#[0-9]+|#x[0-9A-Fa-f]+);")


def _bare_amp(fragment: str) -> bool:
    return "&" in _ENTITY_RE.sub("", fragment)


def find_html_errors(text: str) -> list[str]:
    """Violations of the Telegram HTML subset in text (empty list = valid)."""
    errors: list[str] = []
    stack: list[str] = []
    pos = 0
    while pos < len(text):
        lt = text.find("<", pos)
        plain = text[pos : len(text) if lt < 0 else lt]
        if ">" in plain:
            errors.append(f"unescaped '>' in {plain!r}")
        if _bare_amp(plain):
            errors.append(f"unescaped '&' in {plain!r}")
        if lt < 0:
            break
        m = _TAG_RE.match(text, lt)
        if m is None:
            errors.append(f"unescaped '<' at {lt}: {text[lt : lt + 20]!r}")
            pos = lt + 1
            continue
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name not in ALLOWED_TAGS:
            errors.append(f"unsupported tag <{name}>")
        for attr, value in _ATTR_RE.findall(attrs):
            if name != "a" or attr != "href":
                errors.append(f"attribute {attr} not allowed on <{name}>")
            if _bare_amp(value):
                errors.append(f"unescaped '&' in attribute {attr}")
        if closing:
            if attrs.strip():
                errors.append(f"closing tag </{name}> has attributes")
            if not stack or stack[-1] != name:
                errors.append(f"unbalanced </{name}>")
            else:
                stack.pop()
        else:
            stack.append(name)
        pos = m.end()
    errors.extend(f"unclosed <{name}>" for name in stack)
    return errors


_transports: list["RecordingTransport"] = []


@pytest.fixture(autouse=True)
def _transport_html_valid():
    _transports.clear()
    yield
    bad = [(tr, e) for tr in _transports for e in tr.html_errors]
    _transports.clear()
    assert not bad, "invalid Telegram HTML sent: " + "; ".join(e for _, e in bad)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.html_errors: list[str] = []  # collected, never raised: code may swallow send errors
        self._raise: Exception | None = None
        self._raise_queue: list[Exception] = []
        self._fail_chats: dict[int, Exception] = {}
        self._probe_results: dict[int, Exception] = {}
        self.probes: list[int] = []
        _transports.append(self)

    def set_probe(self, chat_id: int, exc: Exception) -> None:
        # probe_chat(chat_id) raises exc; chats without an entry are healthy
        self._probe_results[chat_id] = exc

    async def probe_chat(self, chat_id: int) -> None:
        self.probes.append(chat_id)
        if chat_id in self._probe_results:
            raise self._probe_results[chat_id]

    def raise_next(self, exc: Exception) -> None:
        self._raise = exc

    def queue_raises(self, excs: list[Exception]) -> None:
        # each call pops the next queued exception, in order, until exhausted
        self._raise_queue.extend(excs)

    def fail_chat(self, chat_id: int, exc: Exception) -> None:
        # every send to this chat_id raises exc until clear_fail_chat is called
        self._fail_chats[chat_id] = exc

    def clear_fail_chat(self, chat_id: int) -> None:
        self._fail_chats.pop(chat_id, None)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        self.html_errors.extend(f"{e} (text={text!r})" for e in find_html_errors(text))
        self.calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "thread_id": thread_id,
            }
        )
        if chat_id in self._fail_chats:
            raise self._fail_chats[chat_id]
        if self._raise_queue:
            raise self._raise_queue.pop(0)
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc


def make_event(**kwargs: Any) -> IncomingEvent:
    defaults: dict[str, Any] = dict(
        kind="message",
        update_id=1,
        chat_id=100,
        chat_type="group",
        user_id=1000,
        username="user",
        display_name="User",
        message_id=1,
        text="",
    )
    defaults.update(kwargs)
    return IncomingEvent(**defaults)


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    database = Database(path)
    await database.connect()
    await apply_migrations(database)
    try:
        yield database
    finally:
        await database.close()


# --- shared runtime-test helpers (plain functions, not fixtures) ---

BOT_ID = 999
BOT_USERNAME = "upb_bot"


def mk_ctx(db, clock=None, *, cooldown: float = 0.0):
    from app.delivery import Delivery
    from app.handlers import Context
    from app.services import Services

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
        ping_cooldown_seconds=cooldown,
    )
    return ctx, services, transport, clock


def _entities(text: str) -> tuple[tuple[str, int, int], ...]:
    first = text.split(" ", 1)[0]
    return (("bot_command", 0, len(first)),)


def group_event(text: str, *, update_id: int, user_id: int, chat_id: int = 500, **kwargs: Any):
    defaults: dict[str, Any] = dict(
        kind="message",
        chat_type="group",
        chat_id=chat_id,
        update_id=update_id,
        user_id=user_id,
        username=f"user{user_id}",
        display_name=f"User{user_id}",
        message_id=update_id,
        text=text,
        entities=_entities(text),
        chat_title="Chat",
    )
    defaults.update(kwargs)
    return make_event(**defaults)


def private_event(text: str, *, update_id: int, user_id: int, **kwargs: Any):
    defaults: dict[str, Any] = dict(
        chat_type="private",
        chat_id=user_id,
        entities=_entities(text) if text.startswith("/") else (),
    )
    defaults.update(kwargs)
    return group_event(text, update_id=update_id, user_id=user_id, **defaults)


async def make_root(db, services, user_id: int, *, contact: bool = False) -> None:
    async with db.transaction() as c:
        await services.touch_user(c, user_id, private_contact=contact)
        await services.set_root(c, user_id)


async def make_admin(db, services, user_id: int) -> None:
    async with db.transaction() as c:
        await services.touch_user(c, user_id)
        await services.grant_admin(c, user_id)


async def register_chat(db, services, chat_id: int, registrar_id: int, title: str = "Chat") -> None:
    async with db.transaction() as c:
        await services.touch_user(c, registrar_id)
        await services.register_chat(c, chat_id, title, registrar_id)


async def subscribe(db, services, chat_id: int, user_id: int) -> None:
    async with db.transaction() as c:
        await services.touch_user(c, user_id, display_name=f"U{user_id}")
        await services.subscribe(c, chat_id, user_id)
