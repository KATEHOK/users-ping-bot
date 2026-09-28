"""aiogram adapter: transport (send_message), update -> IncomingEvent mapping, polling loop.

Nothing here is imported by services.py/delivery.py/handlers/*: this module is the only
place aiogram types are touched, so the rest of the app stays testable without a bot.
"""

import asyncio
import logging
from typing import Literal

from aiogram import Bot, exceptions, types

from .delivery import (
    AmbiguousSend,
    ChatMigrated,
    PermanentSend,
    RateLimited,
    SendError,
    Unauthorized,
    wait_or_stop,
)
from .handlers import Context, handle_event
from .models import IncomingEvent

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 30
POLL_ERROR_BACKOFF = 5.0
MAX_POLL_FAILURES = 10
ALLOWED_UPDATES = ["message", "my_chat_member", "chat_member"]


class PollingFailed(Exception):
    """getUpdates failed MAX_POLL_FAILURES times in a row. Fatal."""


def map_error(exc: BaseException) -> SendError | Unauthorized:
    """Translate an aiogram failure into our own error type.

    Only the exception class and structured numeric fields are read: never str(exc),
    the response body or the request URL (a network error text may carry the token).
    """
    if isinstance(exc, exceptions.TelegramRetryAfter):
        return RateLimited(float(exc.retry_after))
    if isinstance(exc, exceptions.TelegramMigrateToChat):
        return ChatMigrated(int(exc.migrate_to_chat_id))
    if isinstance(exc, exceptions.TelegramUnauthorizedError):
        return Unauthorized()
    if isinstance(
        exc,
        (
            exceptions.TelegramForbiddenError,
            exceptions.TelegramNotFound,
            exceptions.TelegramBadRequest,
        ),
    ):
        return PermanentSend()  # chat gone, bot blocked/kicked, reply target deleted
    return AmbiguousSend()  # network/server/conflict trouble: outcome unclear


class AiogramTransport:
    """Transport backed by a live aiogram Bot; failures are mapped by map_error."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        try:
            await self._bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=thread_id,
            )
        except Exception as exc:
            raise map_error(exc) from None

    async def probe_chat(self, chat_id: int) -> None:
        try:
            member = await self._bot.get_chat_member(chat_id, self._bot.id)
        except Exception as exc:
            raise map_error(exc) from None
        if member.status in ("left", "kicked"):
            raise PermanentSend()


def _display_name(user: types.User) -> str | None:
    parts = [p for p in (user.first_name, user.last_name) if p]
    return " ".join(parts) if parts else None


def _message_event(update_id: int, msg: types.Message, *, edited: bool) -> IncomingEvent:
    chat = msg.chat
    entities = tuple((e.type, e.offset, e.length) for e in (msg.entities or ()))

    if msg.migrate_to_chat_id is not None or msg.migrate_from_chat_id is not None:
        return IncomingEvent(
            kind="message",
            update_id=update_id,
            chat_id=chat.id,
            chat_type=chat.type,
            chat_title=chat.title,
            message_id=msg.message_id,
            migrate_to_chat_id=msg.migrate_to_chat_id,
            migrate_from_chat_id=msg.migrate_from_chat_id,
            edited=edited,
        )

    if msg.left_chat_member is not None:
        return IncomingEvent(
            kind="member_left",
            update_id=update_id,
            chat_id=chat.id,
            chat_type=chat.type,
            chat_title=chat.title,
            message_id=msg.message_id,
            left_user_id=msg.left_chat_member.id,
            edited=edited,
        )

    user_id = None
    username = None
    display_name = None
    is_bot = False
    # sender_chat set => posted as a channel or an anonymous group admin: no identified user
    if msg.sender_chat is None and msg.from_user is not None:
        user_id = msg.from_user.id
        username = msg.from_user.username
        display_name = _display_name(msg.from_user)
        is_bot = msg.from_user.is_bot

    return IncomingEvent(
        kind="message",
        update_id=update_id,
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=chat.title,
        user_id=user_id,
        username=username,
        display_name=display_name,
        is_bot=is_bot,
        message_id=msg.message_id,
        thread_id=msg.message_thread_id if msg.is_topic_message else None,
        text=msg.text,
        entities=entities,
        edited=edited,
    )


def _membership_event(
    update_id: int,
    cmu: types.ChatMemberUpdated,
    *,
    kind: Literal["my_chat_member", "chat_member"],
) -> IncomingEvent:
    chat = cmu.chat
    new_status = cmu.new_chat_member.status
    left = new_status in ("left", "kicked")
    return IncomingEvent(
        kind=kind,
        update_id=update_id,
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=chat.title,
        user_id=cmu.from_user.id if cmu.from_user is not None else None,
        left_user_id=cmu.new_chat_member.user.id if left else None,
        bot_removed=left if kind == "my_chat_member" else False,
    )


def to_event(update: types.Update) -> IncomingEvent | None:
    message = update.message or update.edited_message
    if message is not None:
        return _message_event(update.update_id, message, edited=update.edited_message is not None)
    if update.my_chat_member is not None:
        return _membership_event(update.update_id, update.my_chat_member, kind="my_chat_member")
    if update.chat_member is not None:
        return _membership_event(update.update_id, update.chat_member, kind="chat_member")
    return None  # update kind we did not ask for / do not act on


async def run_polling(
    ctx: Context, bot: Bot, stop: asyncio.Event, *, max_failures: int = MAX_POLL_FAILURES
) -> None:
    """Own controlled long-poll loop, no stored offset.

    The first getUpdates carries no offset: Telegram hands back whatever it still holds
    and claim_update makes replays harmless. Afterwards the offset lives in memory and is
    advanced only once every update of the batch has been handled (committed).
    """
    offset: int | None = None
    failures = 0

    while not stop.is_set():
        poll_task = asyncio.ensure_future(
            bot.get_updates(offset=offset, timeout=POLL_TIMEOUT, allowed_updates=ALLOWED_UPDATES)
        )
        stop_task = asyncio.ensure_future(stop.wait())
        done, _pending = await asyncio.wait(
            {poll_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if poll_task not in done:
            # shutting down: let the in-flight long-poll request drop
            poll_task.cancel()
            stop_task.cancel()
            break
        if not stop_task.done():
            stop_task.cancel()

        try:
            updates = poll_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, exceptions.TelegramUnauthorizedError):
                raise Unauthorized() from None
            failures += 1
            # class only: the text of a network error may contain the request URL
            logger.warning("poll_error exc=%s consecutive=%d", type(exc).__name__, failures)
            if failures >= max_failures:
                raise PollingFailed() from None
            if await wait_or_stop(ctx.clock, stop, POLL_ERROR_BACKOFF):
                break
            continue

        failures = 0
        for update in updates:
            event = to_event(update)
            if event is not None:
                await handle_event(ctx, event)
            offset = update.update_id + 1
