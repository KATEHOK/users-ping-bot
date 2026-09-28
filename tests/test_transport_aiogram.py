"""AiogramTransport against a real aiogram.Bot whose HTTP session is scripted: no network.

Proves the real send_message signature and how real Telegram error bodies map.
"""

import json

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession

from app.delivery import (
    AmbiguousSend,
    ChatMigrated,
    PermanentSend,
    RateLimited,
    Unauthorized,
)
from app.telegram import AiogramTransport

TOKEN = "123456:AAFAKE-TRANSPORT-TOKEN-MARKER"
CHAT = -100


class ScriptedSession(BaseSession):
    """Replays queued (status, body) pairs through aiogram's own response checking."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict]] = []
        self.replies: list[tuple[int, dict]] = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):
        payload = method.model_dump(exclude_none=True)
        self.requests.append((type(method).__name__, payload))
        status, body = self.replies.pop(0)
        response = self.check_response(
            bot=bot, method=method, status_code=status, content=json.dumps(body)
        )
        return response.result


def _sent(chat_id: int = CHAT) -> tuple[int, dict]:
    result = {
        "message_id": 5, "date": 0, "text": "x",
        "chat": {"id": chat_id, "type": "group", "title": "t"},
    }
    return 200, {"ok": True, "result": result}


def _error(status: int, description: str, **parameters) -> tuple[int, dict]:
    body: dict = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return status, body


@pytest.fixture
def session() -> ScriptedSession:
    return ScriptedSession()


@pytest.fixture
def transport(session) -> AiogramTransport:
    bot = Bot(token=TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    return AiogramTransport(bot)


async def test_send_message_builds_the_expected_request(transport, session):
    session.replies.append(_sent())
    await transport.send_message(CHAT, "<b>hi</b>", reply_to_message_id=7, thread_id=9)
    name, payload = session.requests[0]
    assert name == "SendMessage"
    assert payload["chat_id"] == CHAT and payload["text"] == "<b>hi</b>"
    assert payload["parse_mode"] == "HTML"
    assert payload["reply_to_message_id"] == 7
    assert payload["message_thread_id"] == 9
    assert payload["disable_web_page_preview"] is True


async def test_send_message_without_reply_or_thread_omits_them(transport, session):
    session.replies.append(_sent())
    await transport.send_message(CHAT, "hi")
    _name, payload = session.requests[0]
    assert "reply_to_message_id" not in payload and "message_thread_id" not in payload


@pytest.mark.parametrize(
    "reply,expected",
    [
        (_error(400, "Bad Request: message to be replied not found"), PermanentSend),
        (_error(400, "Bad Request: can't parse entities: Unsupported start tag"), PermanentSend),
        (_error(400, "Bad Request: message thread not found"), PermanentSend),
        (_error(403, "Forbidden: bot was kicked from the group chat"), PermanentSend),
        (_error(404, "Not Found"), PermanentSend),
        (_error(502, "Bad Gateway"), AmbiguousSend),
        (_error(409, "Conflict: terminated by other getUpdates request"), AmbiguousSend),
    ],
)
async def test_real_error_bodies_map_to_our_errors(transport, session, reply, expected):
    session.replies.append(reply)
    with pytest.raises(expected) as info:
        await transport.send_message(CHAT, "x")
    assert type(info.value) is expected
    assert TOKEN not in repr(info.value) and info.value.__cause__ is None


async def test_real_429_carries_retry_after(transport, session):
    session.replies.append(_error(429, "Too Many Requests: retry after 7", retry_after=7))
    with pytest.raises(RateLimited) as info:
        await transport.send_message(CHAT, "x")
    assert info.value.retry_after == 7


async def test_real_migration_body_carries_the_new_chat_id(transport, session):
    session.replies.append(
        _error(400, "Bad Request: group chat was upgraded to a supergroup chat",
               migrate_to_chat_id=-1009)
    )
    with pytest.raises(ChatMigrated) as info:
        await transport.send_message(CHAT, "x")
    assert info.value.new_chat_id == -1009


async def test_real_401_is_unauthorized(transport, session):
    session.replies.append(_error(401, "Unauthorized"))
    with pytest.raises(Unauthorized):
        await transport.send_message(CHAT, "x")


def _member(status: str, **extra) -> tuple[int, dict]:
    user = {"id": 123456, "is_bot": True, "first_name": "b"}
    return 200, {"ok": True, "result": {"status": status, "user": user, **extra}}


_NO_RIGHTS = {
    n: False
    for n in (
        "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
        "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
        "can_send_other_messages", "can_add_web_page_previews", "can_change_info",
        "can_invite_users", "can_pin_messages", "can_manage_topics", "can_react_to_messages",
        "can_edit_tag",
    )
}


@pytest.mark.parametrize(
    "reply,gone",
    [
        (_member("member"), False),
        (_member("left"), True),
        (_member("kicked", until_date=0), True),
        (_member("restricted", is_member=False, until_date=0, **_NO_RIGHTS), True),
        (_member("restricted", is_member=True, until_date=0, **_NO_RIGHTS), False),
    ],
)
async def test_probe_chat_through_the_real_bot(transport, session, reply, gone):
    session.replies.append(reply)
    if gone:
        with pytest.raises(PermanentSend):
            await transport.probe_chat(CHAT)
    else:
        await transport.probe_chat(CHAT)
    assert session.requests[0][0] == "GetChatMember"
    assert session.requests[0][1]["user_id"] == 123456  # bot id derived from the token


async def test_probe_chat_errors_map_too(transport, session):
    session.replies.append(_error(400, "Bad Request: chat not found"))
    with pytest.raises(PermanentSend):
        await transport.probe_chat(CHAT)
