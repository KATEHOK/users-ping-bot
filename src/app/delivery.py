"""Send gateway, ping and outbox worker.

A reaction to a command is decided in one transaction (see handlers); what is
sent afterwards is final and is never re-checked. Nothing here holds a database
transaction across an outgoing call. No send error escapes: it is caught and a
safe code is logged. The only exception is Unauthorized, which is fatal.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from . import rendering
from .clock import Clock, SYSTEM_CLOCK, iso
from .db import Database
from .models import DEFAULT_LANG, LANGS, Lang, SubscriberRef
from .services import OutboxEvent, Services

logger = logging.getLogger(__name__)

MAX_PING_RETRIES = 3  # explicit 429s only, per message chunk
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

    # --- replies and ping ---

    async def _apply_migration(self, old_chat_id: int, new_chat_id: int) -> None:
        async with self._db.transaction() as c:
            await self._services.migrate_chat(c, old_chat_id, new_chat_id)

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
        parts = [rendering.mention(r.user_id, r.display_name, r.username) for r in snapshot]
        for text in rendering.split_mentions(parts, limit=chunk_limit, max_items=max_mentions):
            if not await self._send(
                chat_id, text, reply_to=message_id, thread_id=thread_id, code="ping"
            ):
                return  # the remainder is cancelled

    # --- outbox ---

    def _render_outbox_text(self, event: OutboxEvent) -> str | None:
        lang = _payload_lang(event)
        if event.event_type == "chat_farewell":
            return rendering.farewell_text(lang)
        if event.event_type == "root_revoked":
            # created_at is the moment of the revocation, not of a late delivery
            return rendering.root_revoked_text(event.created_at, lang)
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
        async with self._db.transaction() as c:
            if new_chat_id is not None:
                await self._services.migrate_chat(c, event.target_id, new_chat_id)
            await self._services.mark_event(c, event.event_id, status, error=error, retry_at=retry_at)
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
            return False

        text = self._render_outbox_text(event)
        if text is None:
            await self._finish(event, error="unknown_event_type", retryable=False)
            return False

        try:
            await self._transport.send_message(
                event.target_id, text, reply_to_message_id=None, thread_id=None
            )
        except RateLimited as exc:
            await self._finish(event, error="rate_limited", retryable=True, retry_after=exc.retry_after)
            return False
        except ChatMigrated as exc:
            if event.target_kind != "chat":
                await self._finish(event, error="permanent", retryable=False)
            else:
                await self._finish(
                    event, error="chat_migrated", retryable=True, new_chat_id=exc.new_chat_id
                )
            return False
        except PermanentSend:
            await self._finish(event, error="permanent", retryable=False)
            return False
        except AmbiguousSend:
            await self._finish(event, error="ambiguous", retryable=True)
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
            await self.run_outbox_once()
            if await wait_or_stop(self._clock, stop, OUTBOX_POLL_INTERVAL):
                return
