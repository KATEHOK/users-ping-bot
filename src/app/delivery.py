"""Send gateway, ping and outbox worker.

A reaction to a command is decided in one transaction (see handlers); what is
sent afterwards is final and is never re-checked. Nothing here holds a database
transaction across an outgoing call. No send error escapes: it is caught and a
safe code is logged. The only exception is Unauthorized, which is fatal.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from . import rendering
from .clock import Clock, SYSTEM_CLOCK, iso
from .db import Database
from .models import DEFAULT_LANG, LANGS, Lang, SubscriberRef
from .services import OutboxEvent, Services

logger = logging.getLogger(__name__)

MAX_PING_RETRIES = 3  # explicit 429s only, per message chunk
MAX_MENU_ATTEMPTS = 3  # setMyCommands / deleteMyCommands, 429s included
MENU_MAX_WAIT = 5.0  # a longer 429 wait is not worth it for a cosmetic menu: heals at the next start
OUTBOX_MAX_ATTEMPTS = 3
OUTBOX_POLL_INTERVAL = 5.0
OUTBOX_BASE_BACKOFF = 30.0
OUTBOX_MAX_BACKOFF = 3600.0


def _log_code(code: str, **ids: int | None) -> None:
    # fixed literal codes plus ids only: never a token, response body, message text
    # or anything read out of an exception's own message/args
    context = " ".join(f"{k}={v}" for k, v in ids.items() if v is not None)
    logger.info("%s %s", code, context)


class SendError(Exception):
    pass


class RateLimited(SendError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class AmbiguousSend(SendError):
    """Unclear network outcome: the message may or may not have been delivered."""


class PermanentSend(SendError):
    """Chat gone, bot blocked, message deleted: never retried."""


class ChatMigrated(SendError):
    """The group became a supergroup; carries the new chat id."""

    def __init__(self, new_chat_id: int) -> None:
        super().__init__("chat_migrated")
        self.new_chat_id = new_chat_id


class Unauthorized(Exception):
    """The bot token was rejected. Fatal: deliberately not a SendError."""


class Transport(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        thread_id: int | None = None,
    ) -> None: ...

    async def probe_chat(self, chat_id: int) -> None:
        """Return if the bot is still in the chat; raise PermanentSend / ChatMigrated /
        AmbiguousSend otherwise."""
        ...

    async def set_chat_commands(
        self, chat_id: int, commands: Sequence[tuple[str, str]], *, user_id: int | None = None
    ) -> None:
        """setMyCommands for this chat's scope (or one member's in it): (command, description) pairs."""
        ...

    async def delete_chat_commands(self, chat_id: int, *, user_id: int | None = None) -> None:
        """deleteMyCommands for this chat's scope (or one member's in it)."""
        ...


async def wait_or_stop(clock: Clock, stop: asyncio.Event, seconds: float) -> bool:
    """Sleep on the clock but wake at once on stop. True if stop is set."""
    if stop.is_set():
        return True
    sleeper = asyncio.ensure_future(clock.sleep(seconds))
    waiter = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sleeper, waiter):
            if not task.done():
                task.cancel()
    return stop.is_set()


def _outbox_backoff(attempts: int) -> float:
    return min(OUTBOX_BASE_BACKOFF * (2**attempts), OUTBOX_MAX_BACKOFF)


def _payload_lang(event: OutboxEvent) -> Lang:
    lang = event.payload.get("lang")
    return lang if lang in LANGS else DEFAULT_LANG


@dataclass(frozen=True)
class _Outcome:
    """What the status write after one attempt should record."""

    sent: bool = False
    error: str = ""
    retryable: bool = False
    retry_after: float | None = None
    new_chat_id: int | None = None


_MENU_MOVED = ("moved", "kept_destination")  # migrate_chat actions that change registrations


class _Flood:
    """One startup menu batch: set when a menu call meets a 429 longer than MENU_MAX_WAIT."""

    hit = False


class Delivery:
    def __init__(
        self,
        db: Database,
        services: Services,
        transport: Transport,
        *,
        clock: Clock = SYSTEM_CLOCK,
        stop: asyncio.Event | None = None,
    ) -> None:
        self._db = db
        self._services = services
        self._transport = transport
        self._clock = clock
        self.stop = stop if stop is not None else asyncio.Event()
        # An attempt happened but its status write failed: this process never sends the
        # event again, it only retries the write. Memory only, so after a restart the
        # event may go out at most once more.
        self._unwritten: dict[int, _Outcome] = {}

    # --- replies and ping ---

    async def _apply_migration(self, old_chat_id: int, new_chat_id: int) -> None:
        async with self._db.transaction() as c:
            result = await self._services.migrate_chat(c, old_chat_id, new_chat_id)
        if result.action in _MENU_MOVED:
            await self.sync_chat_menus([old_chat_id, new_chat_id], single_attempt=True)

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None,
        thread_id: int | None,
        code: str,
    ) -> bool:
        """One message with up to MAX_PING_RETRIES retries on 429. False = not delivered."""
        retries = 0
        while True:
            try:
                await self._transport.send_message(
                    chat_id, text, reply_to_message_id=reply_to, thread_id=thread_id
                )
                return True
            except RateLimited as exc:
                retries += 1
                if retries > MAX_PING_RETRIES:
                    _log_code(f"{code}_retries_exhausted", chat_id=chat_id)
                    return False
                if await wait_or_stop(self._clock, self.stop, exc.retry_after):
                    _log_code(f"{code}_stopped", chat_id=chat_id)
                    return False
            except ChatMigrated as exc:
                _log_code(f"{code}_chat_migrated", chat_id=chat_id)
                try:
                    await self._apply_migration(chat_id, exc.new_chat_id)
                except Unauthorized:
                    raise
                except Exception as err:
                    logger.error("migration_failed exc=%s", type(err).__name__)
                return False  # the reply / ping is cancelled
            except AmbiguousSend:
                _log_code(f"{code}_ambiguous", chat_id=chat_id)
                return False
            except PermanentSend:
                _log_code(f"{code}_permanent", chat_id=chat_id)
                return False

    async def send_reply(
        self, chat_id: int, text: str, *, reply_to: int | None, thread_id: int | None
    ) -> bool:
        return await self._send(chat_id, text, reply_to=reply_to, thread_id=thread_id, code="reply")

    async def run_ping(
        self,
        chat_id: int,
        message_id: int | None,
        thread_id: int | None,
        snapshot: Sequence[SubscriberRef],
        *,
        chunk_limit: int = rendering.MAX_MESSAGE,
        max_mentions: int = rendering.MAX_MENTIONS,
    ) -> None:
        """Send the ping for a final recipient snapshot; an empty one answers with pong."""
        if not snapshot:
            await self._send(
                chat_id, rendering.PONG, reply_to=message_id, thread_id=thread_id, code="pong"
            )
            return
        parts = [rendering.mention(r.user_id, r.display_name) for r in snapshot]
        for text in rendering.split_mentions(parts, limit=chunk_limit, max_items=max_mentions):
            if not await self._send(
                chat_id, text, reply_to=message_id, thread_id=thread_id, code="ping"
            ):
                return  # the remainder is cancelled

    # --- command menu ---

    async def sync_chat_menu(
        self, chat_id: int, *, single_attempt: bool = False, users: Sequence[int] = ()
    ) -> bool:
        """Make the chat's command menus match the DB.

        Active chat: the common menu for everyone, the owner menu for root and the registrar.
        Inactive: the chat menu is deleted; each of `users` (candidates whose member menu may
        be stale) gets the register menu if still staff, else none. In an active chat a
        candidate who is not an owner loses the member menu.
        Idempotent and never raises (except Unauthorized): failures are logged by code.
        `single_attempt`: no 429 wait or retry, for callers that must not block.
        True if every menu now matches the state.
        """
        return await self._sync_menu(
            chat_id, MAX_MENU_ATTEMPTS if not single_attempt else 1, tuple(users), None
        )

    async def sync_chat_menus(
        self, chat_ids: Sequence[int], *, single_attempt: bool = False, users: Sequence[int] = ()
    ) -> None:
        await self._sync_chats(chat_ids, single_attempt, tuple(users), None)

    async def _sync_chats(
        self,
        chat_ids: Sequence[int],
        single_attempt: bool,
        users: tuple[int, ...],
        flood: _Flood | None,
    ) -> None:
        attempts = MAX_MENU_ATTEMPTS if not single_attempt else 1
        for chat_id in dict.fromkeys(chat_ids):
            if self.stop.is_set():
                return
            await self._sync_menu(chat_id, attempts, users, flood)
            if flood is not None and flood.hit:
                # the flood wait is bot-wide: the rest heals at the next start or change
                _log_code("menu_sync_aborted_rate_limited", chat_id=chat_id)
                return

    async def sync_menus_at_start(
        self, chat_ids: Sequence[int], *, users: Sequence[int] = ()
    ) -> None:
        """Startup batch: sync the chats, then delete the recorded member menus of users
        without a role. A 429 longer than MENU_MAX_WAIT stops the rest of the batch."""
        flood = _Flood()
        await self._sync_chats(chat_ids, False, tuple(users), flood)
        if flood.hit:
            return
        try:
            async with self._db.reader() as c:
                leftovers = await self._services.roleless_member_menus(c)
        except Exception as exc:
            logger.error("menu_state_error exc=%s", type(exc).__name__)
            return
        for chat_id, user_id in leftovers:
            if self.stop.is_set():
                return
            await self._delete_member_menu(chat_id, user_id, MAX_MENU_ATTEMPTS, flood)
            if flood.hit:
                _log_code("menu_sync_aborted_rate_limited", chat_id=chat_id, user_id=user_id)
                return

    async def delete_member_menus(
        self, chat_ids: Sequence[int], user_id: int, *, single_attempt: bool = False
    ) -> None:
        """Delete one user's member-scope menu in each chat (a revoked role's leftovers).

        Failures (e.g. the user is not in the chat) are logged by code; Unauthorized propagates.
        """
        attempts = MAX_MENU_ATTEMPTS if not single_attempt else 1
        for chat_id in dict.fromkeys(chat_ids):
            if self.stop.is_set():
                return
            await self._delete_member_menu(chat_id, user_id, attempts, None)

    async def _delete_member_menu(
        self, chat_id: int, user_id: int, attempts: int, flood: _Flood | None
    ) -> None:
        try:
            async with self._db.reader() as c:
                active = await self._services.get_chat(c, chat_id) is not None
        except Exception as exc:
            logger.error("menu_state_error exc=%s", type(exc).__name__)
            active = False
        done = await self._menu_call(chat_id, user_id, None, active, attempts, flood)
        if done is None:
            # a registered chat had migrated: the registration and the record moved on
            await self._delete_after_migration(chat_id, user_id, attempts)

    async def _delete_after_migration(self, old_chat_id: int, user_id: int, attempts: int) -> None:
        try:
            async with self._db.reader() as c:
                new_chat_id = await self._services.resolve_chat_id(c, old_chat_id)
        except Exception as exc:
            logger.error("menu_state_error exc=%s", type(exc).__name__)
            return
        if new_chat_id != old_chat_id:
            await self._menu_call(new_chat_id, user_id, None, True, attempts)

    async def _note_menu(
        self, chat_id: int, user_id: int, commands: list[tuple[str, str]] | None, active: bool
    ) -> None:
        """Record a member menu after the Telegram call succeeded. A failure is only logged."""
        try:
            async with self._db.transaction() as c:
                if commands is None:
                    await self._services.forget_member_menu(c, chat_id, user_id)
                else:
                    kind = "owner" if active else "register"
                    await self._services.record_member_menu(c, chat_id, user_id, kind)
        except Exception as exc:
            logger.error(
                "menu_record_error exc=%s chat_id=%s user_id=%s", type(exc).__name__, chat_id, user_id
            )

    async def _menu_plan(
        self, chat_id: int, users: tuple[int, ...]
    ) -> tuple[bool, list[tuple[int | None, list[tuple[str, str]] | None]]]:
        """(chat active, [(user_id or None for the chat scope, commands or None = delete)])."""
        async with self._db.reader() as c:
            chat = await self._services.get_chat(c, chat_id)
            svc = self._services
            if chat is not None:
                plan = [(None, rendering.menu_commands(chat.lang))]
                owners: list[int] = []
                root_id = await svc.get_root(c)
                if root_id is not None:
                    owners.append(root_id)
                if await svc.get_role(c, chat.registered_by) is not None:
                    owners.append(chat.registered_by)
                owners = list(dict.fromkeys(owners))
                plan += [(u, rendering.owner_menu_commands(chat.lang)) for u in owners]
                plan += [(u, None) for u in dict.fromkeys(users) if u not in owners]
                return True, plan
            plan = [(None, None)]
            for u in dict.fromkeys(users):
                if await svc.get_role(c, u) is None:
                    plan.append((u, None))
                else:
                    lang = await svc.get_user_lang(c, u)
                    plan.append((u, rendering.register_menu_commands(lang)))
            return False, plan

    async def _sync_menu(
        self, chat_id: int, max_attempts: int, users: tuple[int, ...], flood: _Flood | None
    ) -> bool:
        try:
            active, plan = await self._menu_plan(chat_id, users)
        except Exception as exc:
            logger.error("menu_state_error exc=%s", type(exc).__name__)
            return False
        ok = True
        for user_id, commands in plan:
            done = await self._menu_call(chat_id, user_id, commands, active, max_attempts, flood)
            if done is None:
                return True  # the chat moved: the migration synced both ids
            if not done:
                ok = False
                if user_id is None:
                    break  # the chat itself is out of reach: member menus would fail too
                if flood is not None and flood.hit:
                    break  # the rest of this chat's calls would meet the same flood
        return ok

    async def _menu_call(
        self,
        chat_id: int,
        user_id: int | None,
        commands: list[tuple[str, str]] | None,
        active: bool,
        max_attempts: int,
        flood: _Flood | None = None,
    ) -> bool | None:
        """One set/delete with the 429 policy. None: the chat migrated and was handled."""
        code = "menu_set" if commands is not None else "menu_delete"
        attempts = 0
        while True:
            attempts += 1
            try:
                if commands is not None:
                    await self._transport.set_chat_commands(chat_id, commands, user_id=user_id)
                else:
                    await self._transport.delete_chat_commands(chat_id, user_id=user_id)
                if user_id is not None:
                    await self._note_menu(chat_id, user_id, commands, active)
                return True
            except RateLimited as exc:
                if exc.retry_after > MENU_MAX_WAIT and flood is not None:
                    flood.hit = True
                if attempts >= max_attempts or exc.retry_after > MENU_MAX_WAIT:
                    _log_code(f"{code}_retries_exhausted", chat_id=chat_id, user_id=user_id)
                    return False
                if await wait_or_stop(self._clock, self.stop, exc.retry_after):
                    _log_code(f"{code}_stopped", chat_id=chat_id, user_id=user_id)
                    return False
            except ChatMigrated as exc:
                _log_code(f"{code}_chat_migrated", chat_id=chat_id, user_id=user_id)
                if not active:
                    # an upgraded group has no menu to show; the old id is dead
                    if user_id is not None:
                        await self._note_menu(chat_id, user_id, None, False)
                    return True
                try:
                    # syncs both ids when the registration moved
                    await self._apply_migration(chat_id, exc.new_chat_id)
                except Unauthorized:
                    raise
                except Exception as err:
                    logger.error("migration_failed exc=%s", type(err).__name__)
                    return False
                return None
            except AmbiguousSend:
                _log_code(f"{code}_ambiguous", chat_id=chat_id, user_id=user_id)
                return False
            except PermanentSend:
                # a gone chat, a forbidden bot or a user who is not in the chat
                _log_code(f"{code}_permanent", chat_id=chat_id, user_id=user_id)
                if commands is None and user_id is not None:
                    # the call can never succeed: drop the record instead of retrying forever
                    await self._note_menu(chat_id, user_id, None, False)
                return False

    # --- outbox ---

    def _render_outbox_text(self, event: OutboxEvent) -> str | None:
        lang = _payload_lang(event)
        if event.event_type == "chat_farewell":
            return rendering.farewell_text(lang)
        if event.event_type == "root_revoked":
            # created_at is the moment of the revocation, not of a late delivery
            return rendering.root_revoked_text(event.created_at, lang)
        if event.event_type == "reconcile_removed":
            raw = event.payload.get("chat_ids")
            ids = [i for i in raw if isinstance(i, int)] if isinstance(raw, list) else []
            if event.payload.get("report_partial"):
                return rendering.report_partial_text(ids, lang)
            if event.payload.get("report_lost"):
                return rendering.report_lost_text(ids, lang)
            return rendering.reconcile_interrupted_text(ids, lang)
        return None

    async def _finish(
        self,
        event: OutboxEvent,
        *,
        error: str,
        retryable: bool,
        retry_after: float | None = None,
        new_chat_id: int | None = None,
    ) -> None:
        """Record one failed attempt: retry later, or give up (failed, always logged)."""
        give_up = not retryable or event.attempts + 1 >= OUTBOX_MAX_ATTEMPTS
        if give_up:
            status, retry_at = "failed", None
        else:
            delay = retry_after if retry_after is not None else _outbox_backoff(event.attempts)
            delay = min(delay, OUTBOX_MAX_BACKOFF)
            status, retry_at = "pending", iso(self._clock.now() + timedelta(seconds=delay))
            if new_chat_id is not None:
                retry_at = None  # retry on the new id in the next cycle
        moved = False
        async with self._db.transaction() as c:
            if new_chat_id is not None and new_chat_id != event.target_id:
                # equal ids: the migration is already in place (the row was retargeted)
                result = await self._services.migrate_chat(c, event.target_id, new_chat_id)
                moved = result.action in _MENU_MOVED
            await self._services.mark_event(c, event.event_id, status, error=error, retry_at=retry_at)
        if moved:
            await self.sync_chat_menus([event.target_id, new_chat_id])
        if give_up:
            logger.error(
                "outbox_failed event_id=%s type=%s code=%s attempts=%s",
                event.event_id,
                event.event_type,
                error,
                event.attempts + 1,
            )
        else:
            _log_code("outbox_retry", event_id=event.event_id, chat_id=event.target_id)

    async def _deliver_one(self, stale: OutboxEvent) -> bool:
        # the whole row is re-read: status, target and attempts may have changed
        async with self._db.reader() as c:
            event = await self._services.get_event(c, stale.event_id)
        if event is None or event.status != "pending":
            if event is not None:
                self._unwritten.pop(event.event_id, None)
            return False
        pending = self._unwritten.get(event.event_id)
        if pending is not None:
            await self._record(event, pending)
            return False

        text = self._render_outbox_text(event)
        if text is None:
            await self._record(event, _Outcome(error="unknown_event_type"))
            return False

        try:
            await self._transport.send_message(
                event.target_id, text, reply_to_message_id=None, thread_id=None
            )
        except RateLimited as exc:
            outcome = _Outcome(error="rate_limited", retryable=True, retry_after=exc.retry_after)
        except ChatMigrated as exc:
            if event.target_kind != "chat":
                outcome = _Outcome(error="permanent")
            else:
                outcome = _Outcome(
                    error="chat_migrated", retryable=True, new_chat_id=exc.new_chat_id
                )
        except PermanentSend:
            outcome = _Outcome(error="permanent")
        except AmbiguousSend:
            outcome = _Outcome(error="ambiguous", retryable=True)
        else:
            await self._record(event, _Outcome(sent=True))
            return True
        await self._record(event, outcome)
        return False

    async def _record(self, event: OutboxEvent, outcome: _Outcome) -> None:
        """Write the status of an attempt. On failure remember it: no resend, only the write."""
        try:
            if outcome.sent:
                async with self._db.transaction() as c:
                    await self._services.mark_event(c, event.event_id, "sent")
            else:
                await self._finish(
                    event,
                    error=outcome.error,
                    retryable=outcome.retryable,
                    retry_after=outcome.retry_after,
                    new_chat_id=outcome.new_chat_id,
                )
        except Unauthorized:
            raise
        except Exception as exc:
            self._unwritten[event.event_id] = outcome
            logger.error(
                "outbox_status_write_failed event_id=%s exc=%s", event.event_id, type(exc).__name__
            )
        else:
            self._unwritten.pop(event.event_id, None)
            final = outcome.sent or not outcome.retryable or event.attempts + 1 >= OUTBOX_MAX_ATTEMPTS
            if event.event_type == "chat_farewell" and final:
                # the chat was dropped (maybe by the CLI, which has no transport): drop its menu
                owners = event.payload.get("owners")
                users = [u for u in owners if isinstance(u, int)] if isinstance(owners, list) else []
                try:
                    async with self._db.reader() as c:
                        users += await self._services.staff_ids(c)
                except Exception as exc:
                    logger.error("menu_state_error exc=%s", type(exc).__name__)
                await self.sync_chat_menu(event.target_id, users=users)
            elif event.event_type == "root_revoked" and final:
                await self._sync_after_root_change(event.target_id)

    async def _sync_after_root_change(self, old_root_id: int) -> None:
        """Root changed while the bot runs (via the CLI): drop the old root's recorded menus
        and refresh the rest."""
        try:
            async with self._db.reader() as c:
                recorded = await self._services.member_menu_chats(c, old_root_id)
                chat_ids = await self._services.known_chat_ids(c)
                staff = await self._services.staff_ids(c)
        except Exception as exc:
            logger.error("menu_state_error exc=%s", type(exc).__name__)
            return
        await self.delete_member_menus(recorded, old_root_id)
        await self.sync_chat_menus(chat_ids, users=staff)

    async def run_outbox_once(self) -> int:
        now_iso = iso(self._clock.now())
        async with self._db.reader() as c:
            events = await self._services.due_events(c, now_iso)

        delivered = 0
        for event in events:
            if self.stop.is_set():
                break
            try:
                if await self._deliver_one(event):
                    delivered += 1
            except Unauthorized:
                raise
            except Exception as exc:
                # one bad event must never stop the batch
                logger.error("outbox_unexpected_error event_id=%s exc=%s", event.event_id, type(exc).__name__)
        return delivered

    async def outbox_loop(self, stop: asyncio.Event | None = None) -> None:
        stop = stop if stop is not None else self.stop
        while not stop.is_set():
            try:
                await self.run_outbox_once()
            except Unauthorized:
                raise
            except Exception as exc:
                # e.g. a locked database: try again on the next tick
                logger.error("outbox_loop_error exc=%s", type(exc).__name__)
            if await wait_or_stop(self._clock, stop, OUTBOX_POLL_INTERVAL):
                return
