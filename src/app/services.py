"""All state mutation: identity, updates, chats, subscriptions, roles/cascades.

Every method is async and takes an already-open aiosqlite.Connection as its
first argument after self. Services never opens its own transaction, never
sleeps, never touches Telegram or the network: the caller composes one
atomic unit via Database.transaction()/reader().

This stage implements identity, updates, chats, subscriptions and roles/
cascades only. Outbox, migration and conflict groups are stage 4; call
sites that belong to that stage are marked with a `# stage 4:` comment.
"""

from dataclasses import dataclass, field
from typing import Literal

import aiosqlite

from .clock import Clock, SYSTEM_CLOCK, iso
from .models import Actor, Role, SubscriberRef


@dataclass(frozen=True, slots=True)
class RegisterResult:
    created: bool
    generation: int


@dataclass(frozen=True, slots=True)
class UnregisterResult:
    chat_id: int
    generation: int
    subscriptions_removed: int


@dataclass(frozen=True, slots=True)
class SubscribeResult:
    created: bool
    subscription_id: int


@dataclass(frozen=True, slots=True)
class GrantResult:
    status: Literal["created", "exists", "is_root"]


@dataclass(frozen=True, slots=True)
class RevokeResult:
    revoked: bool  # False if the target was not an admin: no-op
    chat_ids: list[int] = field(default_factory=list)  # chats dropped by the cascade


@dataclass(frozen=True, slots=True)
class RemoveChatResult:
    chat_ids: list[int]  # every chat removed, including the requested one
    admin_demoted: int | None  # user_id demoted from admin, or None (registrar was root)


@dataclass(frozen=True, slots=True)
class SetRootResult:
    changed: bool
    previous_root_id: int | None
    dropped_chat_ids: list[int]
    notified_previous: bool  # True only if the previous root has private_contact_at set


@dataclass(frozen=True, slots=True)
class ChatRow:
    chat_id: int
    title: str | None
    registered_by: int
    registered_at: str
    registration_generation: int
    blocked: bool  # stage 4: always False here; filled from migration_conflicts later


@dataclass(frozen=True, slots=True)
class UserRow:
    user_id: int
    display_name: str | None
    username: str | None


class Services:
    def __init__(self, clock: Clock = SYSTEM_CLOCK) -> None:
        self._clock = clock

    def _now(self) -> str:
        return iso(self._clock.now())

    async def _next_counter(self, c: aiosqlite.Connection, name: str) -> int:
        cursor = await c.execute(
            "UPDATE counters SET value = value + 1 WHERE name = ? RETURNING value",
            (name,),
        )
        row = await cursor.fetchone()
        return row[0]

    async def _ensure_user(self, c: aiosqlite.Connection, user_id: int) -> None:
        await c.execute(
            "INSERT INTO users(user_id, updated_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, self._now()),
        )

    # --- identity ---

    async def touch_user(
        self,
        c: aiosqlite.Connection,
        user_id: int,
        *,
        username: str | None = None,
        display_name: str | None = None,
        private_contact: bool = False,
    ) -> None:
        now = self._now()
        contact_at = now if private_contact else None
        await c.execute(
            "INSERT INTO users(user_id, username, display_name, private_contact_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username = COALESCE(excluded.username, users.username), "
            "display_name = COALESCE(excluded.display_name, users.display_name), "
            # private_contact_at is sticky: set once, never overwritten once recorded.
            "private_contact_at = COALESCE(users.private_contact_at, excluded.private_contact_at), "
            "updated_at = excluded.updated_at",
            (user_id, username, display_name, contact_at, now),
        )

    async def get_role(self, c: aiosqlite.Connection, user_id: int) -> Role | None:
        cursor = await c.execute("SELECT role FROM roles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return Role(row[0]) if row is not None else None

    async def load_actor(
        self, c: aiosqlite.Connection, user_id: int, *, chat_id: int | None = None
    ) -> Actor:
        role = await self.get_role(c, user_id)
        is_subscriber = False
        if chat_id is not None:
            cursor = await c.execute(
                "SELECT 1 FROM subscriptions WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            is_subscriber = await cursor.fetchone() is not None
        return Actor(user_id=user_id, role=role, is_subscriber=is_subscriber)

    # --- updates / polling ---

    async def claim_update(
        self, c: aiosqlite.Connection, bot_id: int, update_id: int, outcome: str = "ok"
    ) -> bool:
        cursor = await c.execute(
            "INSERT INTO processed_updates(bot_id, update_id, processed_at, outcome) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(bot_id, update_id) DO NOTHING",
            (bot_id, update_id, self._now(), outcome),
        )
        return cursor.rowcount == 1

    async def get_offset(self, c: aiosqlite.Connection, bot_id: int) -> int:
        cursor = await c.execute(
            "SELECT next_offset FROM polling_state WHERE bot_id = ?", (bot_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row is not None else 0

    async def set_offset(self, c: aiosqlite.Connection, bot_id: int, offset: int) -> None:
        await c.execute(
            "INSERT INTO polling_state(bot_id, next_offset, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(bot_id) DO UPDATE SET "
            "next_offset = excluded.next_offset, updated_at = excluded.updated_at",
            (bot_id, offset, self._now()),
        )

    # --- chats ---

    async def get_chat(self, c: aiosqlite.Connection, chat_id: int) -> ChatRow | None:
        cursor = await c.execute(
            "SELECT chat_id, title, registered_by, registered_at, registration_generation "
            "FROM chats WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ChatRow(
            chat_id=row[0],
            title=row[1],
            registered_by=row[2],
            registered_at=row[3],
            registration_generation=row[4],
            blocked=False,
        )

    async def register_chat(
        self, c: aiosqlite.Connection, chat_id: int, title: str | None, registrar_id: int
    ) -> RegisterResult:
        existing = await self.get_chat(c, chat_id)
        if existing is not None:
            # idempotent: registrar, generation and subscriptions are untouched
            return RegisterResult(created=False, generation=existing.registration_generation)
        generation = await self._next_counter(c, "generation")
        await c.execute(
            "INSERT INTO chats(chat_id, title, registered_by, registered_at, registration_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, title, registrar_id, self._now(), generation),
        )
        # stage 4: cancel pending farewells of this chat and its aliases (older generations)
        return RegisterResult(created=True, generation=generation)

    async def unregister_chat(
        self, c: aiosqlite.Connection, chat_id: int, *, farewell: bool = True
    ) -> UnregisterResult:
        existing = await self.get_chat(c, chat_id)
        if existing is None:
            return UnregisterResult(chat_id=chat_id, generation=0, subscriptions_removed=0)
        cursor = await c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE chat_id = ?", (chat_id,)
        )
        (removed,) = await cursor.fetchone()
        # deleting the chat row cascades subscriptions via ON DELETE CASCADE
        await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        if farewell:
            pass  # stage 4: queue a farewell outbox event for chat_id
        return UnregisterResult(
            chat_id=chat_id,
            generation=existing.registration_generation,
            subscriptions_removed=removed,
        )

    async def list_chats(self, c: aiosqlite.Connection) -> list[ChatRow]:
        cursor = await c.execute(
            "SELECT chat_id, title, registered_by, registered_at, registration_generation "
            "FROM chats ORDER BY chat_id"
        )
        rows = await cursor.fetchall()
        return [
            ChatRow(
                chat_id=r[0],
                title=r[1],
                registered_by=r[2],
                registered_at=r[3],
                registration_generation=r[4],
                blocked=False,
            )
            for r in rows
        ]

    # --- subscriptions ---

    async def subscribe(
        self, c: aiosqlite.Connection, chat_id: int, user_id: int
    ) -> SubscribeResult:
        cursor = await c.execute(
            "SELECT subscription_id FROM subscriptions WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return SubscribeResult(created=False, subscription_id=row[0])
        subscription_id = await self._next_counter(c, "subscription_id")
        await c.execute(
            "INSERT INTO subscriptions(chat_id, user_id, subscription_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, user_id, subscription_id, self._now()),
        )
        return SubscribeResult(created=True, subscription_id=subscription_id)

    async def unsubscribe(self, c: aiosqlite.Connection, chat_id: int, user_id: int) -> bool:
        cursor = await c.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        return cursor.rowcount == 1

    async def list_subscribers(
        self, c: aiosqlite.Connection, chat_id: int
    ) -> list[SubscriberRef]:
        cursor = await c.execute(
            "SELECT s.user_id, s.subscription_id, u.display_name, u.username "
            "FROM subscriptions s JOIN users u ON u.user_id = s.user_id "
            "WHERE s.chat_id = ? ORDER BY s.user_id",
            (chat_id,),
        )
        rows = await cursor.fetchall()
        return [
            SubscriberRef(user_id=r[0], subscription_id=r[1], display_name=r[2], username=r[3])
            for r in rows
        ]

    async def subscription_id_of(
        self, c: aiosqlite.Connection, chat_id: int, user_id: int
    ) -> int | None:
        cursor = await c.execute(
            "SELECT subscription_id FROM subscriptions WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
        return row[0] if row is not None else None

    # --- roles / cascades ---

    async def grant_admin(self, c: aiosqlite.Connection, user_id: int) -> GrantResult:
        role = await self.get_role(c, user_id)
        if role is Role.ROOT:
            return GrantResult(status="is_root")
        if role is Role.ADMIN:
            return GrantResult(status="exists")
        # admin can be granted by id before the user ever contacted the bot
        await self._ensure_user(c, user_id)
        await c.execute(
            "INSERT INTO roles(user_id, role, granted_at) VALUES (?, 'admin', ?)",
            (user_id, self._now()),
        )
        return GrantResult(status="created")

    async def revoke_admin(self, c: aiosqlite.Connection, user_id: int) -> RevokeResult:
        role = await self.get_role(c, user_id)
        if role is not Role.ADMIN:
            return RevokeResult(revoked=False, chat_ids=[])
        cursor = await c.execute("SELECT chat_id FROM chats WHERE registered_by = ?", (user_id,))
        chat_ids = [r[0] for r in await cursor.fetchall()]
        await c.execute("DELETE FROM roles WHERE user_id = ?", (user_id,))
        for chat_id in chat_ids:
            # each delete cascades only that chat's own subscriptions (invariant 11):
            # U's subscriptions in chats registered by someone else are untouched
            await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            # stage 4: queue a farewell outbox event for chat_id
        return RevokeResult(revoked=True, chat_ids=chat_ids)

    async def list_admins(self, c: aiosqlite.Connection) -> list[UserRow]:
        cursor = await c.execute(
            "SELECT u.user_id, u.display_name, u.username FROM roles r "
            "JOIN users u ON u.user_id = r.user_id "
            "WHERE r.role = 'admin' ORDER BY u.user_id"
        )
        rows = await cursor.fetchall()
        return [UserRow(user_id=r[0], display_name=r[1], username=r[2]) for r in rows]

    async def get_root(self, c: aiosqlite.Connection) -> int | None:
        cursor = await c.execute("SELECT user_id FROM roles WHERE role = 'root'")
        row = await cursor.fetchone()
        return row[0] if row is not None else None

    async def remove_chat_cascade(
        self, c: aiosqlite.Connection, chat_id: int
    ) -> RemoveChatResult:
        chat = await self.get_chat(c, chat_id)
        if chat is None:
            return RemoveChatResult(chat_ids=[], admin_demoted=None)
        registrar_role = await self.get_role(c, chat.registered_by)
        if registrar_role is Role.ADMIN:
            # registrar is admin: drop the admin role and every chat he registered, C included
            result = await self.revoke_admin(c, chat.registered_by)
            return RemoveChatResult(chat_ids=result.chat_ids, admin_demoted=chat.registered_by)
        # registrar is root (or has no role left): remove only the requested chat
        await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        # stage 4: queue a farewell outbox event for chat_id
        return RemoveChatResult(chat_ids=[chat_id], admin_demoted=None)

    async def set_root(self, c: aiosqlite.Connection, new_root_id: int) -> SetRootResult:
        previous_root_id = await self.get_root(c)
        if previous_root_id == new_root_id:
            return SetRootResult(
                changed=False,
                previous_root_id=previous_root_id,
                dropped_chat_ids=[],
                notified_previous=False,
            )

        await self._ensure_user(c, new_root_id)

        dropped_chat_ids: list[int] = []
        notified_previous = False
        if previous_root_id is not None:
            cursor = await c.execute(
                "SELECT chat_id FROM chats WHERE registered_by = ?", (previous_root_id,)
            )
            dropped_chat_ids = [r[0] for r in await cursor.fetchall()]
            for chat_id in dropped_chat_ids:
                await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
                # stage 4: queue a farewell outbox event for chat_id
            # delete the old root's role row before inserting the new one, so the
            # roles_single_root partial unique index is never transiently violated
            await c.execute("DELETE FROM roles WHERE user_id = ?", (previous_root_id,))
            cursor = await c.execute(
                "SELECT private_contact_at FROM users WHERE user_id = ?", (previous_root_id,)
            )
            row = await cursor.fetchone()
            notified_previous = row is not None and row[0] is not None
            # stage 4: queue a personal outbox notification to previous_root_id
            # when notified_previous is True

        # set the new user's role to exactly root; if he was admin, this replaces
        # that row in place, so every chat he registered as admin is kept as-is
        await c.execute(
            "INSERT INTO roles(user_id, role, granted_at) VALUES (?, 'root', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET role = 'root', granted_at = excluded.granted_at",
            (new_root_id, self._now()),
        )

        return SetRootResult(
            changed=True,
            previous_root_id=previous_root_id,
            dropped_chat_ids=dropped_chat_ids,
            notified_previous=notified_previous,
        )
