"""All state mutation: identity, updates, chats, subscriptions, roles/cascades,
outbox and chat migration.

Every method is async and takes an already-open aiosqlite.Connection as its
first argument after self. Services never opens its own transaction, never
sleeps, never touches Telegram or the network: the caller composes one
atomic unit via Database.transaction()/reader().
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import aiosqlite

from .clock import Clock, SYSTEM_CLOCK, iso
from .models import DEFAULT_LANG, LANGS, Actor, Lang, Role, SubscriberRef

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegisterResult:
    created: bool
    generation: int


@dataclass(frozen=True, slots=True)
class UnregisterResult:
    chat_id: int
    generation: int
    owner_ids: tuple[int, ...] = ()  # who held owner menus: root and the registrar


@dataclass(frozen=True, slots=True)
class SubscribeResult:
    created: bool


@dataclass(frozen=True, slots=True)
class GrantResult:
    status: Literal["created", "exists", "is_root"]


@dataclass(frozen=True, slots=True)
class RevokeResult:
    revoked: bool  # False if the target was not an admin: no-op
    chat_ids: list[int] = field(default_factory=list)  # chats dropped by the cascade
    owner_ids: tuple[int, ...] = ()  # menu candidates for those chats: root and the admin


@dataclass(frozen=True, slots=True)
class RemoveChatResult:
    chat_ids: list[int]  # [chat_id] if a registration was removed, else []
    owner_ids: tuple[int, ...] = ()


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
    lang: Lang


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
    action: Literal["moved", "kept_destination", "alias_only", "noop", "contradictory"]


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
        is_chat_owner = False
        if chat_id is not None:
            is_subscriber = await self.is_subscribed(c, chat_id, user_id)
            chat = await self.get_chat(c, chat_id)
            if chat is not None:
                is_chat_owner = role is Role.ROOT or (
                    role is Role.ADMIN and chat.registered_by == user_id
                )
        return Actor(
            user_id=user_id, role=role, is_subscriber=is_subscriber, is_chat_owner=is_chat_owner
        )

    async def has_private_contact(self, c: aiosqlite.Connection, user_id: int) -> bool:
        cursor = await c.execute(
            "SELECT private_contact_at FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row is not None and row[0] is not None

    async def get_user_lang(self, c: aiosqlite.Connection, user_id: int) -> Lang:
        cursor = await c.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return self._lang(row[0] if row is not None else None)

    async def set_user_lang(self, c: aiosqlite.Connection, user_id: int, lang: Lang) -> None:
        self._check_lang(lang)
        await self._ensure_user(c, user_id)
        await c.execute(
            "UPDATE users SET lang = ?, updated_at = ? WHERE user_id = ?",
            (lang, self._now(), user_id),
        )

    @staticmethod
    def _check_lang(lang: str) -> None:
        if lang not in LANGS:
            raise ValueError(f"unsupported language: {lang!r}")

    @staticmethod
    def _lang(raw: str | None) -> Lang:
        return raw if raw in LANGS else DEFAULT_LANG  # type: ignore[return-value]

    # --- updates ---

    async def claim_update(
        self, c: aiosqlite.Connection, bot_id: int, update_id: int
    ) -> bool:
        cursor = await c.execute(
            "INSERT INTO processed_updates(bot_id, update_id, processed_at, outcome) "
            "VALUES (?, ?, ?, 'ok') ON CONFLICT(bot_id, update_id) DO NOTHING",
            (bot_id, update_id, self._now()),
        )
        return cursor.rowcount == 1

    async def set_update_outcome(
        self, c: aiosqlite.Connection, bot_id: int, update_id: int,
        outcome: Literal["ok", "ignored", "error"],
    ) -> None:
        await c.execute(
            "UPDATE processed_updates SET outcome = ? WHERE bot_id = ? AND update_id = ?",
            (outcome, bot_id, update_id),
        )

    async def record_update_outcome(
        self, c: aiosqlite.Connection, bot_id: int, update_id: int,
        outcome: Literal["ok", "ignored", "error"],
    ) -> None:
        """Upsert: works whether or not the claim row exists (it may have rolled back)."""
        await c.execute(
            "INSERT INTO processed_updates(bot_id, update_id, processed_at, outcome) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(bot_id, update_id) "
            "DO UPDATE SET outcome = excluded.outcome",
            (bot_id, update_id, self._now(), outcome),
        )

    async def prune_processed_updates(self, c: aiosqlite.Connection, *, older_than: str) -> int:
        cursor = await c.execute(
            "DELETE FROM processed_updates WHERE processed_at < ?", (older_than,)
        )
        return cursor.rowcount

    async def prune_outbox(self, c: aiosqlite.Connection, *, older_than: str) -> int:
        """Deletes finished events (sent, failed, cancelled) last touched before older_than."""
        cursor = await c.execute(
            "DELETE FROM outbox WHERE status IN ('sent', 'failed', 'cancelled') AND updated_at < ?",
            (older_than,),
        )
        return cursor.rowcount

    # --- chats ---

    _CHAT_COLS = "chat_id, title, registered_by, registered_at, registration_generation, lang"

    @staticmethod
    def _chat_row(r: tuple) -> ChatRow:
        return ChatRow(
            chat_id=r[0],
            title=r[1],
            registered_by=r[2],
            registered_at=r[3],
            registration_generation=r[4],
            lang=Services._lang(r[5]),
        )

    async def get_chat(self, c: aiosqlite.Connection, chat_id: int) -> ChatRow | None:
        cursor = await c.execute(
            f"SELECT {self._CHAT_COLS} FROM chats WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        return self._chat_row(row) if row is not None else None

    async def set_chat_lang(self, c: aiosqlite.Connection, chat_id: int, lang: Lang) -> None:
        self._check_lang(lang)
        await c.execute("UPDATE chats SET lang = ? WHERE chat_id = ?", (lang, chat_id))

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
            return UnregisterResult(chat_id=chat_id, generation=0)
        owners = await self._owner_ids(c, existing.registered_by)
        # deleting the chat row cascades subscriptions via ON DELETE CASCADE
        await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        if farewell:
            await self._queue_farewell(
                c, chat_id, existing.registration_generation, existing.lang, owners
            )
        return UnregisterResult(
            chat_id=chat_id,
            generation=existing.registration_generation,
            owner_ids=owners,
        )

    async def _owner_ids(
        self, c: aiosqlite.Connection, registrar_id: int, *extra: int
    ) -> tuple[int, ...]:
        """Root, the registrar and extras, without repeats: whose menus a chat change touches."""
        root_id = await self.get_root(c)
        ids = [i for i in (root_id, registrar_id, *extra) if i is not None]
        return tuple(dict.fromkeys(ids))

    async def list_chats(
        self, c: aiosqlite.Connection, *, registered_by: int | None = None
    ) -> list[ChatRow]:
        sql = f"SELECT {self._CHAT_COLS} FROM chats"
        params: tuple = ()
        if registered_by is not None:
            sql += " WHERE registered_by = ?"
            params = (registered_by,)
        cursor = await c.execute(sql + " ORDER BY chat_id", params)
        return [self._chat_row(r) for r in await cursor.fetchall()]

    # --- subscriptions ---

    async def subscribe(
        self, c: aiosqlite.Connection, chat_id: int, user_id: int
    ) -> SubscribeResult:
        cursor = await c.execute(
            "INSERT INTO subscriptions(chat_id, user_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO NOTHING",
            (chat_id, user_id, self._now()),
        )
        return SubscribeResult(created=cursor.rowcount == 1)

    async def unsubscribe(self, c: aiosqlite.Connection, chat_id: int, user_id: int) -> bool:
        cursor = await c.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        return cursor.rowcount == 1

    async def is_subscribed(self, c: aiosqlite.Connection, chat_id: int, user_id: int) -> bool:
        cursor = await c.execute(
            "SELECT 1 FROM subscriptions WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        return await cursor.fetchone() is not None

    async def list_subscribers(
        self, c: aiosqlite.Connection, chat_id: int, *, exclude_user_id: int | None = None
    ) -> list[SubscriberRef]:
        sql = (
            "SELECT s.user_id, u.display_name, u.username "
            "FROM subscriptions s JOIN users u ON u.user_id = s.user_id WHERE s.chat_id = ?"
        )
        params: list[int] = [chat_id]
        if exclude_user_id is not None:
            sql += " AND s.user_id != ?"
            params.append(exclude_user_id)
        cursor = await c.execute(sql + " ORDER BY s.user_id", params)
        return [
            SubscriberRef(user_id=r[0], display_name=r[1], username=r[2])
            for r in await cursor.fetchall()
        ]

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
            "SELECT chat_id, registration_generation, lang FROM chats "
            "WHERE registered_by = ? ORDER BY chat_id",
            (user_id,),
        )
        rows = await cursor.fetchall()
        chat_ids = [r[0] for r in rows]
        owners = await self._owner_ids(c, user_id)
        await c.execute("DELETE FROM roles WHERE user_id = ?", (user_id,))
        for chat_id, generation, lang in rows:
            # each delete cascades only that chat's own subscriptions:
            # U's subscriptions in chats registered by someone else are untouched
            await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            await self._queue_farewell(c, chat_id, generation, self._lang(lang), owners)
        return RevokeResult(revoked=True, chat_ids=chat_ids, owner_ids=owners)

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

    async def staff_ids(self, c: aiosqlite.Connection) -> list[int]:
        """Root first, then every admin: all users who may hold a menu of ours."""
        cursor = await c.execute("SELECT user_id FROM roles ORDER BY role = 'root' DESC, user_id")
        return [r[0] for r in await cursor.fetchall()]

    async def former_root_ids(self, c: aiosqlite.Connection) -> list[int]:
        """Users who were told their root role was revoked: they may still hold an owner menu."""
        cursor = await c.execute(
            "SELECT DISTINCT target_id FROM outbox "
            "WHERE event_type = 'root_revoked' AND target_kind = 'user' ORDER BY 1"
        )
        return [r[0] for r in await cursor.fetchall()]

    async def known_chat_ids(self, c: aiosqlite.Connection) -> list[int]:
        """Chat ids to sync at startup: registered or an outbox target (old migrated ids have no menu)."""
        cursor = await c.execute(
            "SELECT chat_id FROM chats "
            "UNION SELECT target_id FROM outbox WHERE target_kind = 'chat' ORDER BY 1"
        )
        return [r[0] for r in await cursor.fetchall()]

    async def remove_chat(self, c: aiosqlite.Connection, chat_id: int) -> RemoveChatResult:
        # only this chat; the registrar keeps their role and other chats
        canonical = await self.resolve_chat_id(c, chat_id)
        result = await self.unregister_chat(c, canonical)
        return RemoveChatResult(
            chat_ids=[canonical] if result.generation else [], owner_ids=result.owner_ids
        )

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
                "SELECT chat_id, registration_generation, lang FROM chats "
                "WHERE registered_by = ? ORDER BY chat_id",
                (previous_root_id,),
            )
            rows = await cursor.fetchall()
            dropped_chat_ids = [r[0] for r in rows]
            for chat_id, generation, lang in rows:
                await c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
                await self._queue_farewell(
                    c, chat_id, generation, self._lang(lang), (previous_root_id, new_root_id)
                )
            # delete the old root's role row before inserting the new one, so the
            # roles_single_root partial unique index is never transiently violated
            await c.execute("DELETE FROM roles WHERE user_id = ?", (previous_root_id,))
            cursor = await c.execute(
                "SELECT private_contact_at FROM users WHERE user_id = ?", (previous_root_id,)
            )
            row = await cursor.fetchone()
            notified_previous = row is not None and row[0] is not None
            previous_lang = await self.get_user_lang(c, previous_root_id)
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
                    payload={"lang": previous_lang},
                )

        # a user who becomes root again no longer needs the "root revoked" notice
        await c.execute(
            "UPDATE outbox SET status = 'cancelled', updated_at = ? "
            "WHERE event_type = 'root_revoked' AND target_kind = 'user' "
            "AND target_id = ? AND status = 'pending'",
            (self._now(), new_root_id),
        )

        if previous_root_id is not None:
            await self._retarget_reconcile_notices(c, previous_root_id, new_root_id)

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

    async def _retarget_reconcile_notices(
        self, c: aiosqlite.Connection, old_root_id: int, new_root_id: int
    ) -> None:
        """Pending removed-chats notices go to the new root, or are cancelled if it cannot be reached."""
        cursor = await c.execute(
            "SELECT event_id, payload FROM outbox WHERE event_type = 'reconcile_removed' "
            "AND target_kind = 'user' AND target_id = ? AND status = 'pending'",
            (old_root_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return
        reachable = await self.has_private_contact(c, new_root_id)
        lang = await self.get_user_lang(c, new_root_id) if reachable else None
        for event_id, raw in rows:
            if reachable:
                payload = json.loads(raw)
                payload["lang"] = lang
                await c.execute(
                    "UPDATE outbox SET target_id = ?, payload = ?, updated_at = ? WHERE event_id = ?",
                    (new_root_id, json.dumps(payload), self._now(), event_id),
                )
            else:
                await c.execute(
                    "UPDATE outbox SET status = 'cancelled', updated_at = ? WHERE event_id = ?",
                    (self._now(), event_id),
                )

    # --- outbox ---

    async def _queue_farewell(
        self,
        c: aiosqlite.Connection,
        chat_id: int,
        generation: int,
        lang: Lang,
        owners: Sequence[int] = (),
    ) -> None:
        # farewell:<chat_id>:<generation> is deterministic and collision-free: a chat
        # only ever gets one farewell per registration generation, and generation is
        # a monotonic counter shared by all chats. The chat row is gone by delivery
        # time, so its language and the owners whose menus need a refresh travel in the payload.
        await self.queue_event(
            c,
            event_key=f"farewell:{chat_id}:{generation}",
            event_type="chat_farewell",
            target_kind="chat",
            target_id=chat_id,
            generation=generation,
            payload={"lang": lang, "owners": list(owners)},
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
        # Cancellation across generations: a chat's pending
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

    _EVENT_COLS = (
        "event_id, event_key, event_type, target_kind, target_id, generation, "
        "payload, status, attempts, next_attempt_at, created_at, updated_at, last_error"
    )

    @staticmethod
    def _event(r: tuple) -> OutboxEvent:
        return OutboxEvent(
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

    async def due_events(
        self, c: aiosqlite.Connection, now_iso: str, limit: int = 10
    ) -> list[OutboxEvent]:
        cursor = await c.execute(
            f"SELECT {self._EVENT_COLS} FROM outbox WHERE status = 'pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY event_id ASC LIMIT ?",
            (now_iso, limit),
        )
        return [self._event(r) for r in await cursor.fetchall()]

    async def get_event(self, c: aiosqlite.Connection, event_id: int) -> OutboxEvent | None:
        cursor = await c.execute(
            f"SELECT {self._EVENT_COLS} FROM outbox WHERE event_id = ?", (event_id,)
        )
        row = await cursor.fetchone()
        return self._event(row) if row is not None else None

    async def mark_event(
        self,
        c: aiosqlite.Connection,
        event_id: int,
        status: str,
        *,
        error: str | None = None,
        retry_at: str | None = None,
    ) -> None:
        # error is a short safe code (e.g. "rate_limited", "permanent", "ambiguous"), never
        # a response body or secret; attempts counts this call as one delivery try.
        await c.execute(
            "UPDATE outbox SET status = ?, attempts = attempts + 1, next_attempt_at = ?, "
            "last_error = ?, updated_at = ? WHERE event_id = ?",
            (status, retry_at, error, self._now(), event_id),
        )

    # --- migration ---

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
        if old_chat_id == new_chat_id:
            return MigrationResult(action="noop")  # never alias a chat to itself
        cursor = await c.execute(
            "SELECT new_chat_id FROM chat_aliases WHERE old_chat_id = ?", (old_chat_id,)
        )
        existing_alias = await cursor.fetchone()
        if existing_alias is not None:
            if existing_alias[0] != new_chat_id:
                log.warning(
                    "contradictory migration: chat %s already aliased to %s, ignoring %s",
                    old_chat_id,
                    existing_alias[0],
                    new_chat_id,
                )
                return MigrationResult(action="contradictory")
            return MigrationResult(action="noop")

        now = self._now()
        await c.execute(
            "INSERT INTO chat_aliases(old_chat_id, new_chat_id, created_at) VALUES (?, ?, ?)",
            (old_chat_id, new_chat_id, now),
        )

        old_row = await self.get_chat(c, old_chat_id)
        dest_row = await self.get_chat(c, new_chat_id)
        action: Literal["moved", "kept_destination", "alias_only"]
        if old_row is None:
            action = "alias_only"
        elif dest_row is None:
            action = "moved"
            # new row first, then move subscriptions, then drop the old row (FKs)
            await c.execute(
                "INSERT INTO chats(chat_id, title, registered_by, registered_at, "
                "registration_generation, lang) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_chat_id,
                    title if title is not None else old_row.title,
                    old_row.registered_by,
                    old_row.registered_at,
                    old_row.registration_generation,
                    old_row.lang,
                ),
            )
            await c.execute(
                "UPDATE subscriptions SET chat_id = ? WHERE chat_id = ?",
                (new_chat_id, old_chat_id),
            )
            await c.execute("DELETE FROM chats WHERE chat_id = ?", (old_chat_id,))
        else:
            action = "kept_destination"
            log.warning(
                "migration %s -> %s: destination already registered, dropping the old registration",
                old_chat_id,
                new_chat_id,
            )
            # subscriptions of the old chat go with it; no farewell to a chat that moved
            await c.execute("DELETE FROM chats WHERE chat_id = ?", (old_chat_id,))

        # re-target undelivered group outbox events, registered or not
        await c.execute(
            "UPDATE outbox SET target_id = ?, updated_at = ? "
            "WHERE target_kind = 'chat' AND status = 'pending' AND target_id = ?",
            (new_chat_id, now, old_chat_id),
        )

        # recorded personal menus follow the chat (a later delete at the new id is harmless)
        await c.execute(
            "UPDATE OR IGNORE member_menus SET chat_id = ? WHERE chat_id = ?",
            (new_chat_id, old_chat_id),
        )
        await c.execute("DELETE FROM member_menus WHERE chat_id = ?", (old_chat_id,))

        return MigrationResult(action=action)

    # --- personal command menus ---

    async def record_member_menu(
        self, c: aiosqlite.Connection, chat_id: int, user_id: int, kind: str
    ) -> None:
        """A member-scope menu was set (`owner` or `register`): upsert."""
        await c.execute(
            "INSERT INTO member_menus(chat_id, user_id, kind, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET kind = excluded.kind, "
            "updated_at = excluded.updated_at",
            (chat_id, user_id, kind, self._now()),
        )

    async def forget_member_menu(self, c: aiosqlite.Connection, chat_id: int, user_id: int) -> None:
        await c.execute(
            "DELETE FROM member_menus WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )

    async def member_menu_chats(self, c: aiosqlite.Connection, user_id: int) -> list[int]:
        """Chats (registered or not) where this user has a recorded personal menu."""
        cursor = await c.execute(
            "SELECT chat_id FROM member_menus WHERE user_id = ? ORDER BY chat_id", (user_id,)
        )
        return [r[0] for r in await cursor.fetchall()]
