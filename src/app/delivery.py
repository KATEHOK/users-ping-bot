"""Send gateway, ping job and outbox worker.

Nothing here holds a database transaction open across an outgoing call: every
re-check below is a short `Database.reader()` snapshot, and every network
attempt happens outside it (plan section 9, "the last-check boundary"). A
value carried in memory (a snapshot, a job, a grant) is never by itself
permission to send; the state is re-read from the database immediately
before each attempt.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import aiosqlite

from . import rendering
from .clock import Clock, SYSTEM_CLOCK, iso
from .db import Database
from .models import SubscriberRef
from .services import OutboxEvent, Services

logger = logging.getLogger(__name__)

MAX_PING_RETRIES = 3          # explicit 429s only; ambiguous outcomes never retry
GRANT_TTL_SECONDS = 60.0
GRANT_MAX_RETRIES = 5         # defensive cap alongside the TTL window (plan leaves it time-bound)
OUTBOX_POLL_INTERVAL = 5.0
OUTBOX_BASE_BACKOFF = 30.0
OUTBOX_MAX_BACKOFF = 3600.0


def _log_event(code: str, **ids: int | None) -> None:
    # fixed literal codes plus ids only: never a token, response body or message
    # text, and never anything read out of an exception's own message/args
    context = " ".join(f"{k}={v}" for k, v in ids.items() if v is not None)
    logger.info("%s %s", code, context)


class SendError(Exception):
    pass


class RateLimited(SendError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class AmbiguousSend(SendError):
    """Unclear network outcome: message may or may not have been delivered. Never auto-retried."""


class PermanentSend(SendError):
    """Chat gone, bot blocked, message deleted: never retried."""


class Transport(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        thread_id: int | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class NotifyOffGrant:
    update_id: int
    user_id: int
    chat_id: int
    message_id: int
    generation: int
    expires_at: datetime


class GrantRegistry:
    """One single-use confirmation slot per successful `notify off`, in memory only.

    Living only in memory is itself what makes a restart revoke every grant.
    """

    def __init__(self, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._clock = clock
        self._by_update: dict[int, NotifyOffGrant] = {}
        self._by_chat: dict[int, set[int]] = {}

    def issue(
        self, *, update_id: int, user_id: int, chat_id: int, message_id: int, generation: int
    ) -> NotifyOffGrant:
        grant = NotifyOffGrant(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            generation=generation,
            expires_at=self._clock.now() + timedelta(seconds=GRANT_TTL_SECONDS),
        )
        self._by_update[update_id] = grant
        self._by_chat.setdefault(chat_id, set()).add(update_id)
        return grant

    def take(self, update_id: int) -> NotifyOffGrant | None:
        # single-use: removed on first take no matter the outcome, so a replayed
        # update_id can never collect a grant twice, and no call mints a new one
        grant = self._by_update.pop(update_id, None)
        if grant is None:
            return None
        self._by_chat.get(grant.chat_id, set()).discard(update_id)
        if self._clock.now() >= grant.expires_at:
            return None  # window closed: consumed, but nothing to send
        return grant

    def revoke_chat(self, chat_id: int) -> None:
        for update_id in self._by_chat.pop(chat_id, set()):
            self._by_update.pop(update_id, None)

    def revoke_user(self, chat_id: int, user_id: int) -> None:
        # narrower than revoke_chat: used when that one user re-subscribes
        # before his own confirmation went out (plan section 9)
        stale = [
            update_id
            for update_id in self._by_chat.get(chat_id, set())
            if self._by_update[update_id].user_id == user_id
        ]
        for update_id in stale:
            self._by_update.pop(update_id, None)
            self._by_chat[chat_id].discard(update_id)


def _peel_ping_chunk(
    refs: Sequence[SubscriberRef], *, limit: int
) -> tuple[list[SubscriberRef], list[SubscriberRef]]:
    """Split off the leading refs that fit in one rendering.split_mentions chunk.

    Mirrors split_mentions' own greedy fill so the boundary used for the
    per-chunk re-check lines up with the message actually built from it.
    """
    parts = [rendering.mention(r.user_id, r.display_name, r.username) for r in refs]
    count = 0
    current_len = 0
    for part in parts:
        extra = len(part) + (1 if count else 0)
        if count and current_len + extra > limit:
            break
        current_len += extra
        count += 1
    if refs and count == 0:
        count = 1  # a single oversized mention still must not be dropped
    return list(refs[:count]), list(refs[count:])


def _outbox_backoff(attempts: int) -> float:
    return min(OUTBOX_BASE_BACKOFF * (2**attempts), OUTBOX_MAX_BACKOFF)


class Delivery:
    def __init__(
        self,
        db: Database,
        services: Services,
        transport: Transport,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._db = db
        self._services = services
        self._transport = transport
        self._clock = clock
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self.grants = GrantRegistry(clock=clock)

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _send_locked(
        self, chat_id: int, text: str, *, reply_to: int | None, thread_id: int | None
    ) -> None:
        # at most one outgoing send per chat at a time; the lock wraps only this
        # single call, never backoff or the surrounding loop (plan 9.3 rule 6)
        async with self._chat_lock(chat_id):
            await self._transport.send_message(
                chat_id, text, reply_to_message_id=reply_to, thread_id=thread_id
            )

    async def send_reply(
        self, chat_id: int, text: str, *, reply_to: int | None, thread_id: int | None
    ) -> None:
        await self._send_locked(chat_id, text, reply_to=reply_to, thread_id=thread_id)

    def cancel_chat(self, chat_id: int) -> None:
        # a chat id that just stopped being relevant (unregistered / migrated /
        # conflicted) loses its grants and its cached lock; a fresh lock is
        # created on demand under whichever id is canonical next, never renamed
        self.grants.revoke_chat(chat_id)
        self._chat_locks.pop(chat_id, None)

    # --- ping (plan section 9.3) ---

    async def _ping_may_continue(
        self, c: aiosqlite.Connection, chat_id: int, initiator_id: int, generation: int
    ) -> bool:
        chat = await self._services.get_chat(c, chat_id)
        if chat is None or chat.registration_generation != generation:
            return False  # unregistered, or migrated/re-registered under a new generation
        if chat.blocked:
            return False  # open migration conflict
        actor = await self._services.load_actor(c, initiator_id, chat_id=chat_id)
        # admin/root keeps the ground to continue even after unsubscribing himself
        return actor.is_staff or actor.is_subscriber

    async def _still_subscribed(
        self, c: aiosqlite.Connection, chat_id: int, refs: list[SubscriberRef]
    ) -> list[SubscriberRef]:
        kept: list[SubscriberRef] = []
        for ref in refs:
            current = await self._services.subscription_id_of(c, chat_id, ref.user_id)
            if current == ref.subscription_id:
                kept.append(ref)
        return kept

    async def _send_with_retries(
        self,
        chat_id: int,
        text: str,
        message_id: int | None,
        thread_id: int | None,
        *,
        initiator_id: int,
        generation: int,
        event_code: str,
    ) -> bool:
        retries = 0
        while True:
            try:
                await self._send_locked(chat_id, text, reply_to=message_id, thread_id=thread_id)
                return True
            except RateLimited as exc:
                retries += 1
                if retries > MAX_PING_RETRIES:
                    _log_event(f"{event_code}_retries_exhausted", chat_id=chat_id)
                    return False
                # the wait always precedes the re-check, never the other way around
                await self._clock.sleep(exc.retry_after)
                async with self._db.reader() as c:
                    if not await self._ping_may_continue(c, chat_id, initiator_id, generation):
                        return False
            except AmbiguousSend:
                _log_event(f"{event_code}_ambiguous", chat_id=chat_id)
                return False
            except PermanentSend:
                _log_event(f"{event_code}_permanent", chat_id=chat_id)
                return False

    async def run_ping(
        self,
        chat_id: int,
        initiator_id: int,
        message_id: int,
        thread_id: int | None,
        generation: int,
        snapshot: Sequence[SubscriberRef],
        *,
        chunk_limit: int = rendering.MAX_MESSAGE,
    ) -> None:
        if not snapshot:
            await self._send_with_retries(
                chat_id,
                rendering.PONG,
                message_id,
                thread_id,
                initiator_id=initiator_id,
                generation=generation,
                event_code="ping_pong",
            )
            return

        remaining = list(snapshot)
        while remaining:
            # re-filter the not-yet-sent remainder to still-existing (user_id,
            # subscription_id) pairs, and re-check generation/conflict/grounds,
            # before building the next chunk (plan 9.3 rules 2-3)
            async with self._db.reader() as c:
                if not await self._ping_may_continue(c, chat_id, initiator_id, generation):
                    return
                remaining = await self._still_subscribed(c, chat_id, remaining)
            if not remaining:
                return  # emptied mid-way: silence, never a trailing pong

            head, remaining = _peel_ping_chunk(remaining, limit=chunk_limit)
            parts = [rendering.mention(r.user_id, r.display_name, r.username) for r in head]
            text = rendering.split_mentions(parts, limit=chunk_limit)[0]

            sent = await self._send_with_retries(
                chat_id,
                text,
                message_id,
                thread_id,
                initiator_id=initiator_id,
                generation=generation,
                event_code="ping_chunk",
            )
            if not sent:
                return
            await asyncio.sleep(0)  # let incoming on/off/unregister keep being recorded

    async def confirm_notify_off(self, grant: NotifyOffGrant, text: str) -> bool:
        """Send the one allowed notify-off confirmation for `grant`, or drop it.

        Re-checks the last-check boundary (generation currency, conflict,
        remaining window) before every attempt; a success or an ambiguous
        outcome never retries, an explicit 429 may retry inside the window.
        """
        retries = 0
        while True:
            if self._clock.now() >= grant.expires_at:
                return False  # window closed
            async with self._db.reader() as c:
                chat = await self._services.get_chat(c, grant.chat_id)
                if chat is None or chat.registration_generation != grant.generation or chat.blocked:
                    return False
            try:
                await self._send_locked(
                    grant.chat_id, text, reply_to=grant.message_id, thread_id=None
                )
                return True
            except RateLimited as exc:
                retries += 1
                if retries > GRANT_MAX_RETRIES:
                    return False
                await self._clock.sleep(exc.retry_after)
                continue  # loop re-checks expiry and generation before retrying
            except (AmbiguousSend, PermanentSend):
                return False

    # --- outbox (plan sections 7-8) ---

    def _render_outbox_text(self, event: OutboxEvent) -> str | None:
        if event.event_type == "chat_farewell":
            return rendering.farewell_text()
        if event.event_type == "root_revoked":
            # the event's own created_at is the revocation's time/context, so a
            # late delivery still names the moment rather than asserting "now"
            return rendering.root_revoked_text(event.created_at)
        return None

    async def _deliver_one(self, event: OutboxEvent) -> bool:
        async with self._db.reader() as c:
            status = await self._services.event_status(c, event.event_id)
        if status != "pending":
            return False  # cancelled/sent/failed since due_events fetched it

        text = self._render_outbox_text(event)
        if text is None:
            async with self._db.transaction() as c:
                await self._services.mark_event(
                    c, event.event_id, "failed", error="unknown_event_type"
                )
            _log_event("outbox_unknown_type", event_id=event.event_id)
            return False

        # farewells and root notices are standalone: no reply, so they survive
        # a migration retarget of target_id without a stale reply/thread
        try:
            await self._send_locked(event.target_id, text, reply_to=None, thread_id=None)
        except PermanentSend:
            async with self._db.transaction() as c:
                await self._services.mark_event(c, event.event_id, "failed", error="permanent")
            _log_event("outbox_permanent", event_id=event.event_id, chat_id=event.target_id)
            return False
        except RateLimited:
            retry_at = iso(self._clock.now() + timedelta(seconds=_outbox_backoff(event.attempts)))
            async with self._db.transaction() as c:
                await self._services.mark_event(
                    c, event.event_id, "pending", error="rate_limited", retry_at=retry_at
                )
            _log_event("outbox_retry", event_id=event.event_id, chat_id=event.target_id)
            return False
        except AmbiguousSend:
            retry_at = iso(self._clock.now() + timedelta(seconds=_outbox_backoff(event.attempts)))
            async with self._db.transaction() as c:
                await self._services.mark_event(
                    c, event.event_id, "pending", error="ambiguous", retry_at=retry_at
                )
            _log_event("outbox_retry", event_id=event.event_id, chat_id=event.target_id)
            return False

        async with self._db.transaction() as c:
            await self._services.mark_event(c, event.event_id, "sent")
        return True

    async def run_outbox_once(self) -> int:
        now_iso = iso(self._clock.now())
        async with self._db.reader() as c:
            events = await self._services.due_events(c, now_iso)

        delivered = 0
        for event in events:
            try:
                if await self._deliver_one(event):
                    delivered += 1
            except Exception:
                # one bad event must never stop the batch; only a short code and
                # the event_id are logged, never anything from the failure itself
                _log_event("outbox_unexpected_error", event_id=event.event_id)
        return delivered

    async def outbox_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_outbox_once()
            sleeper = asyncio.ensure_future(self._clock.sleep(OUTBOX_POLL_INTERVAL))
            waiter = asyncio.ensure_future(stop.wait())
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
            for task in (sleeper, waiter):
                if not task.done():
                    task.cancel()
