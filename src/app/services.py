"""All state mutation: identity, updates, chats, subscriptions, roles/cascades,
outbox and chat migration/conflicts.

Every method is async and takes an already-open aiosqlite.Connection as its
first argument after self. Services never opens its own transaction, never
sleeps, never touches Telegram or the network: the caller composes one
atomic unit via Database.transaction()/reader().
"""

import json
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
    blocked: bool  # True if chat_id's alias group appears in an open migration_conflicts row


@dataclass(frozen=True, slots=True)
class UserRow:
    user_id: int
    display_name: str | None
    username: str | None


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    event_id: int
    event_key: str
    event_type: str
    target_kind: Literal["chat", "user"]
    target_id: int
    generation: int | None
    payload: dict
    status: str
    attempts: int
    next_attempt_at: str | None
    created_at: str
    updated_at: str
    last_error: str | None


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: bool
    conflict_id: int | None


@dataclass(frozen=True, slots=True)
class ConflictRow:
    conflict_id: int
    chat_ids: list[int]
    reason: str
    details: str | None
    status: str
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class ConflictScope:
    chat_ids: list[int]
    alias_pairs: list[tuple[int, int]]  # (old_chat_id, new_chat_id)
    conflict_ids: list[int]


@dataclass(frozen=True, slots=True)
class ResetResult:
    conflict_ids: list[int]
    chat_ids: list[int]
    chats_removed: int
    subscriptions_removed: int
    events_cancelled: int
    aliases_removed: int


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
            blocked=await self.is_blocked(c, row[0]),
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
        # a fresh registration supersedes any earlier generation's undelivered
        # farewell for this chat_id and every alias that still maps into it
        await self.cancel_chat_events(c, chat_id)
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
            await self._queue_farewell(c, chat_id, existing.registration_generation)
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
                blocked=await self.is_blocked(c, r[0]),
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
        cursor = await c.execute(
            "SELECT chat_id, registration_generation FROM chats WHERE registered_by = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        chat_ids = [r[0] for r in rows]
        generations = {r[0]: r[1] for r in rows}
        await c.execute("DELETE FROM roles WHERE user_id = ?", (user_id,))
        for chat_id in chat_ids:
            # each delete cascades only that chat's own subscriptions (invariant 11):
            # U's subscriptions in chats registered by someone else are untouched
            await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            await self._queue_farewell(c, chat_id, generations[chat_id])
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
        await self._queue_farewell(c, chat_id, chat.registration_generation)
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
                "SELECT chat_id, registration_generation FROM chats WHERE registered_by = ?",
                (previous_root_id,),
            )
            rows = await cursor.fetchall()
            dropped_chat_ids = [r[0] for r in rows]
            generations = {r[0]: r[1] for r in rows}
            for chat_id in dropped_chat_ids:
                await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
                await self._queue_farewell(c, chat_id, generations[chat_id])
            # delete the old root's role row before inserting the new one, so the
            # roles_single_root partial unique index is never transiently violated
            await c.execute("DELETE FROM roles WHERE user_id = ?", (previous_root_id,))
            cursor = await c.execute(
                "SELECT private_contact_at FROM users WHERE user_id = ?", (previous_root_id,)
            )
            row = await cursor.fetchone()
            notified_previous = row is not None and row[0] is not None
            if notified_previous:
                # personal, one-off notice to the demoted root; not a group farewell,
                # so it is keyed by a timestamp rather than a registration generation
                await self.queue_event(
                    c,
                    event_key=f"root_revoked:{previous_root_id}:{self._now()}",
                    event_type="root_revoked",
                    target_kind="user",
                    target_id=previous_root_id,
                    generation=None,
                    payload={},
                )

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

    # --- outbox ---

    async def _queue_farewell(self, c: aiosqlite.Connection, chat_id: int, generation: int) -> None:
        # farewell:<chat_id>:<generation> is deterministic and collision-free: a chat
        # only ever gets one farewell per registration generation, and generation is
        # a monotonic counter shared by all chats, so the key never repeats by chance.
        await self.queue_event(
            c,
            event_key=f"farewell:{chat_id}:{generation}",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=chat_id,
            generation=generation,
            payload={},
        )

    async def queue_event(
        self,
        c: aiosqlite.Connection,
        *,
        event_key: str,
        event_type: str,
        target_kind: Literal["chat", "user"],
        target_id: int,
        generation: int | None = None,
        payload: dict,
    ) -> int | None:
        now = self._now()
        cursor = await c.execute(
            "INSERT INTO outbox(event_key, event_type, target_kind, target_id, generation, "
            "payload, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?) "
            "ON CONFLICT(event_key) DO NOTHING",
            (event_key, event_type, target_kind, target_id, generation, json.dumps(payload), now, now),
        )
        if cursor.rowcount == 0:
            return None  # event_key already present: deduplicated
        return cursor.lastrowid

    async def _alias_group(self, c: aiosqlite.Connection, chat_id: int) -> set[int]:
        """All ids whose alias chain resolves into chat_id, chat_id included.

        Reverse BFS over chat_aliases: each step adds the old_chat_id side of
        every edge pointing at an id already in the group. Only ever grows with
        ids not yet seen, so a cycle (which resolve_chat_id already guards
        against) cannot loop here either.
        """
        group = {chat_id}
        frontier = [chat_id]
        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            cursor = await c.execute(
                f"SELECT old_chat_id FROM chat_aliases WHERE new_chat_id IN ({placeholders})",
                frontier,
            )
            next_frontier = [r[0] for r in await cursor.fetchall() if r[0] not in group]
            group.update(next_frontier)
            frontier = next_frontier
        return group

    async def cancel_chat_events(self, c: aiosqlite.Connection, chat_id: int) -> int:
        # Cancellation-across-generations rule (plan section 8): a chat's pending
        # group events are matched by its whole alias group, not just the literal
        # id passed in, so a stale farewell queued under an old id is still found
        # after the chat has since migrated or been re-registered under aliases.
        canonical = await self.resolve_chat_id(c, chat_id)
        group = await self._alias_group(c, canonical)
        placeholders = ",".join("?" for _ in group)
        cursor = await c.execute(
            f"UPDATE outbox SET status = 'cancelled', updated_at = ? "
            f"WHERE target_kind = 'chat' AND status = 'pending' AND target_id IN ({placeholders})",
            [self._now(), *group],
        )
        return cursor.rowcount

    async def due_events(
        self, c: aiosqlite.Connection, now_iso: str, limit: int = 10
    ) -> list[OutboxEvent]:
        cursor = await c.execute(
            "SELECT event_id, event_key, event_type, target_kind, target_id, generation, "
            "payload, status, attempts, next_attempt_at, created_at, updated_at, last_error "
            "FROM outbox WHERE status = 'pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY event_id ASC LIMIT ?",
            (now_iso, limit),
        )
        rows = await cursor.fetchall()
        return [
            OutboxEvent(
                event_id=r[0],
                event_key=r[1],
                event_type=r[2],
                target_kind=r[3],
                target_id=r[4],
                generation=r[5],
                payload=json.loads(r[6]),
                status=r[7],
                attempts=r[8],
                next_attempt_at=r[9],
                created_at=r[10],
                updated_at=r[11],
                last_error=r[12],
            )
            for r in rows
        ]

    async def event_status(self, c: aiosqlite.Connection, event_id: int) -> str | None:
        cursor = await c.execute("SELECT status FROM outbox WHERE event_id = ?", (event_id,))
        row = await cursor.fetchone()
        return row[0] if row is not None else None

    async def mark_event(
        self,
        c: aiosqlite.Connection,
        event_id: int,
        status: str,
        *,
        error: str | None = None,
        retry_at: str | None = None,
    ) -> None:
        # error is a short safe code (e.g. "rate_limited", "chat_not_found"), never
        # a response body or secret; attempts counts this call as one delivery try.
        await c.execute(
            "UPDATE outbox SET status = ?, attempts = attempts + 1, next_attempt_at = ?, "
            "last_error = ?, updated_at = ? WHERE event_id = ?",
            (status, retry_at, error, self._now(), event_id),
        )

    # --- migration / conflicts ---

    async def resolve_chat_id(self, c: aiosqlite.Connection, chat_id: int) -> int:
        current = chat_id
        seen = {current}
        while True:
            cursor = await c.execute(
                "SELECT new_chat_id FROM chat_aliases WHERE old_chat_id = ?", (current,)
            )
            row = await cursor.fetchone()
            if row is None:
                return current
            nxt = row[0]
            if nxt in seen:
                return current  # cycle guard: stop at the last id seen before repeating
            seen.add(nxt)
            current = nxt

    async def migrate_chat(
        self,
        c: aiosqlite.Connection,
        old_chat_id: int,
        new_chat_id: int,
        *,
        title: str | None = None,
    ) -> MigrationResult:
        cursor = await c.execute(
            "SELECT new_chat_id FROM chat_aliases WHERE old_chat_id = ?", (old_chat_id,)
        )
        existing_alias = await cursor.fetchone()
        if existing_alias is not None:
            if existing_alias[0] == new_chat_id:
                return MigrationResult(applied=False, conflict_id=None)  # repeat: no-op
            conflict_id = await self.open_conflict(
                c,
                [old_chat_id, new_chat_id, existing_alias[0]],
                "contradictory_alias",
                details=f"old_chat_id {old_chat_id} already aliased to {existing_alias[0]}",
            )
            return MigrationResult(applied=False, conflict_id=conflict_id)

        dest_existing = await self.get_chat(c, new_chat_id)
        if dest_existing is not None:
            conflict_id = await self.open_conflict(
                c,
                [old_chat_id, new_chat_id],
                "destination_registration_exists",
                details=f"new_chat_id {new_chat_id} already has an independent registration",
            )
            return MigrationResult(applied=False, conflict_id=conflict_id)

        now = self._now()
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (?, ?, ?)",
            (old_chat_id, new_chat_id, now),
        )

        old_row = await self.get_chat(c, old_chat_id)
        if old_row is not None:
            # insert the new row, then move subscriptions onto it, then drop the old
            # row: this order keeps every subscription_id and never trips the FK
            await c.execute(
                "INSERT INTO chats(chat_id, title, registered_by, registered_at, "
                "registration_generation) VALUES (?, ?, ?, ?, ?)",
                (
                    new_chat_id,
                    title if title is not None else old_row.title,
                    old_row.registered_by,
                    old_row.registered_at,
                    old_row.registration_generation,
                ),
            )
            await c.execute(
                "UPDATE subscriptions SET chat_id = ? WHERE chat_id = ?",
                (new_chat_id, old_chat_id),
            )
            await c.execute("DELETE FROM chats WHERE chat_id = ?", (old_chat_id,))

        # re-target undelivered group outbox events, active registration or not:
        # a pending farewell must reach the chat under its new address too
        await c.execute(
            "UPDATE outbox SET target_id = ?, updated_at = ? "
            "WHERE target_kind = 'chat' AND status = 'pending' AND target_id = ?",
            (new_chat_id, now, old_chat_id),
        )

        return MigrationResult(applied=True, conflict_id=None)

    async def open_conflict(
        self,
        c: aiosqlite.Connection,
        chat_ids: list[int],
        reason: str,
        details: str | None = None,
    ) -> int:
        ids = sorted(set(chat_ids))
        cursor = await c.execute(
            "INSERT INTO migration_conflicts(chat_ids, reason, details, status, created_at) "
            "VALUES (?, ?, ?, 'open', ?)",
            (json.dumps(ids), reason, details, self._now()),
        )
        conflict_id = cursor.lastrowid
        for chat_id in ids:
            # same transaction: nothing in this scope keeps sending while the
            # conflict is being recorded
            await self.cancel_chat_events(c, chat_id)
        return conflict_id

    async def is_blocked(self, c: aiosqlite.Connection, chat_id: int) -> bool:
        canonical = await self.resolve_chat_id(c, chat_id)
        group = await self._alias_group(c, canonical)
        cursor = await c.execute(
            "SELECT chat_ids FROM migration_conflicts WHERE status = 'open'"
        )
        rows = await cursor.fetchall()
        for (chat_ids_json,) in rows:
            if group & set(json.loads(chat_ids_json)):
                return True
        return False

    async def list_conflicts(
        self, c: aiosqlite.Connection, *, status: str = "open"
    ) -> list[ConflictRow]:
        cursor = await c.execute(
            "SELECT conflict_id, chat_ids, reason, details, status, created_at, resolved_at "
            "FROM migration_conflicts WHERE status = ? ORDER BY conflict_id",
            (status,),
        )
        rows = await cursor.fetchall()
        return [
            ConflictRow(
                conflict_id=r[0],
                chat_ids=json.loads(r[1]),
                reason=r[2],
                details=r[3],
                status=r[4],
                created_at=r[5],
                resolved_at=r[6],
            )
            for r in rows
        ]

    async def conflict_scope(self, c: aiosqlite.Connection, conflict_id: int) -> ConflictScope:
        cursor = await c.execute(
            "SELECT chat_ids FROM migration_conflicts WHERE conflict_id = ?", (conflict_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"unknown conflict_id {conflict_id}")

        ids: set[int] = set(json.loads(row[0]))
        conflict_ids: set[int] = {conflict_id}
        alias_pairs: set[tuple[int, int]] = set()

        # Fixpoint expansion: pull in alias edges touching the current id set, then
        # any other open conflict overlapping it, repeating until nothing new turns
        # up. This is the full connected group, no more (no hidden expansion into
        # unrelated ids) and no less (no silent narrowing of an overlapping conflict).
        changed = True
        while changed:
            changed = False
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cursor = await c.execute(
                    f"SELECT old_chat_id, new_chat_id FROM chat_aliases "
                    f"WHERE old_chat_id IN ({placeholders}) OR new_chat_id IN ({placeholders})",
                    [*ids, *ids],
                )
                for old_id, new_id in await cursor.fetchall():
                    if (old_id, new_id) not in alias_pairs:
                        alias_pairs.add((old_id, new_id))
                    if old_id not in ids:
                        ids.add(old_id)
                        changed = True
                    if new_id not in ids:
                        ids.add(new_id)
                        changed = True

            cursor = await c.execute(
                "SELECT conflict_id, chat_ids FROM migration_conflicts WHERE status = 'open'"
            )
            for other_id, other_json in await cursor.fetchall():
                other_ids = set(json.loads(other_json))
                if other_ids & ids and other_id not in conflict_ids:
                    conflict_ids.add(other_id)
                    changed = True
                    ids |= other_ids

        return ConflictScope(
            chat_ids=sorted(ids),
            alias_pairs=sorted(alias_pairs),
            conflict_ids=sorted(conflict_ids),
        )

    async def reset_conflict(self, c: aiosqlite.Connection, conflict_id: int) -> ResetResult:
        scope = await self.conflict_scope(c, conflict_id)

        chats_removed = 0
        subscriptions_removed = 0
        events_cancelled = 0
        for chat_id in scope.chat_ids:
            cursor = await c.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE chat_id = ?", (chat_id,)
            )
            (sub_count,) = await cursor.fetchone()
            cursor = await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            chats_removed += cursor.rowcount
            subscriptions_removed += sub_count  # cascade-deleted along with the chats row
            # idempotent: only 'pending' events are touched, so overlapping alias
            # groups across several chat_ids in scope are never double-counted
            events_cancelled += await self.cancel_chat_events(c, chat_id)

        for old_id, new_id in scope.alias_pairs:
            await c.execute(
                "DELETE FROM chat_aliases WHERE old_chat_id = ? AND new_chat_id = ?",
                (old_id, new_id),
            )

        now = self._now()
        for cid in scope.conflict_ids:
            await c.execute(
                "UPDATE migration_conflicts SET status = 'resolved', resolved_at = ? "
                "WHERE conflict_id = ?",
                (now, cid),
            )

        return ResetResult(
            conflict_ids=scope.conflict_ids,
            chat_ids=scope.chat_ids,
            chats_removed=chats_removed,
            subscriptions_removed=subscriptions_removed,
            events_cancelled=events_cancelled,
            aliases_removed=len(scope.alias_pairs),
        )
