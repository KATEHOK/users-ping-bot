from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

Lang = Literal["en", "ru"]
LANGS: tuple[Lang, ...] = ("en", "ru")
DEFAULT_LANG: Lang = "en"


class Role(StrEnum):
    ROOT = "root"
    ADMIN = "admin"


class Scope(StrEnum):
    GROUP = "group"
    PRIVATE = "private"


class Cmd(StrEnum):
    # group
    CHAT_REGISTER = "chat_register"
    CHAT_UNREGISTER = "chat_unregister"
    NOTIFY_ON = "notify_on"
    NOTIFY_OFF = "notify_off"
    PING = "ping"
    LIST = "list"
    HELP = "help"
    LANG = "lang"
    USAGE = "usage"  # bare/partial/unknown /upb input: help for the typed prefix
    # private
    P_HELP = "p_help"
    ADMIN_CREATE = "admin_create"
    ADMIN_REMOVE = "admin_remove"
    ADMIN_LIST = "admin_list"
    CHAT_LIST = "chat_list"
    CHAT_REMOVE = "chat_remove"
    P_LANG = "p_lang"
    P_USAGE = "p_usage"  # bare/partial /admin, /chat, /lang: help for the typed prefix


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: int
    role: Role | None = None
    is_subscriber: bool = False  # subscriber of the chat in question
    is_chat_owner: bool = False  # root, or the admin who registered the chat in question

    @property
    def is_root(self) -> bool:
        return self.role is Role.ROOT

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def is_staff(self) -> bool:
        return self.is_root or self.is_admin


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    cmd: Cmd
    args: tuple[str, ...]
    via_alias: bool = field(default=False, compare=False)  # typed as a short group form


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    kind: Literal["message", "my_chat_member", "chat_member", "member_left"]
    update_id: int
    chat_id: int
    chat_type: Literal["private", "group", "supergroup", "channel"]
    chat_title: str | None = None
    user_id: int | None = None  # None => anonymous/sender_chat/channel
    username: str | None = None
    display_name: str | None = None
    is_bot: bool = False
    message_id: int | None = None
    thread_id: int | None = None
    text: str | None = None
    entities: tuple[tuple[str, int, int], ...] = ()  # (type, offset, length)
    edited: bool = False
    # membership / migration
    left_user_id: int | None = None
    bot_removed: bool = False
    bot_added: bool = False  # my_chat_member: the bot became a member/administrator
    migrate_to_chat_id: int | None = None
    migrate_from_chat_id: int | None = None


@dataclass(frozen=True, slots=True)
class SubscriberRef:
    user_id: int
    display_name: str | None = None
    username: str | None = None
