import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from .clock import iso

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: str, *, busy_timeout_ms: int = 5000) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._path not in (":memory:", ""):
            parent = os.path.dirname(self._path)
            if parent:
                Path(parent).mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=FULL")
        await self._conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        assert self._conn is not None, "database not connected"
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                await self._conn.rollback()
                raise
            else:
                await self._conn.commit()

    @asynccontextmanager
    async def reader(self) -> AsyncIterator[aiosqlite.Connection]:
        assert self._conn is not None, "database not connected"
        async with self._lock:
            yield self._conn


@asynccontextmanager
async def open_database(path: str) -> AsyncIterator[Database]:
    db = Database(path)
    await db.connect()
    try:
        await apply_migrations(db)
        yield db
    finally:
        await db.close()


def _split_statements(sql: str) -> list[str]:
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


async def apply_migrations(db: Database) -> list[str]:
    async with db.transaction() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        cursor = await conn.execute("SELECT version FROM schema_migrations")
        existing = {row[0] for row in await cursor.fetchall()}

    applied: list[str] = []
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = file.stem
        if version in existing:
            continue
        statements = _split_statements(file.read_text())
        async with db.transaction() as conn:
            for stmt in statements:
                await conn.execute(stmt)
            await conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, iso(datetime.now(timezone.utc))),
            )
        applied.append(version)
    return applied
