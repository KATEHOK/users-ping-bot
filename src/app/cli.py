"""Local operator CLI: root assignment and migration-conflict maintenance.

A separate process/connection from the bot: it only uses `config.load_db_path`
and `db.open_database`, so it applies/verifies the schema itself and works
while the bot is stopped, with no bot token and no Vault reachable. It must
never import vault, telegram, __main__ or aiogram (enforced by
tests/test_cli.py). All state changes go through services.py; this module
never re-implements a cascade, it only calls into Services and formats the
result.
"""

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Coroutine, Sequence

import aiosqlite

from . import config
from .db import MIGRATIONS_DIR, open_database
from .services import ConflictScope, ResetResult, Services, SetRootResult


class CliError(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {raw!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_set_root = sub.add_parser("set-root", help="assign the global root user")
    p_set_root.add_argument("telegram_user_id", type=_positive_int)

    p_mc = sub.add_parser("migration-conflicts", help="inspect/reset migration conflicts")
    mc_sub = p_mc.add_subparsers(dest="action", required=True)

    mc_sub.add_parser("list", help="list open conflicts")

    p_show = mc_sub.add_parser("show", help="show the full connected scope of a conflict")
    p_show.add_argument("conflict_id", type=_positive_int)

    p_reset = mc_sub.add_parser("reset", help="preview or apply a conflict reset")
    p_reset.add_argument("conflict_id", type=_positive_int)
    p_reset.add_argument("--apply", action="store_true")

    p_backup = sub.add_parser(
        "backup", help="write a consistent snapshot of the live database via the backup API"
    )
    p_backup.add_argument("destination_path")
    p_backup.add_argument("--force", action="store_true", help="overwrite an existing destination")

    p_verify = sub.add_parser(
        "verify", help="check that a backup file is a usable database with the expected schema"
    )
    p_verify.add_argument("path")

    return parser


def _format_ids(ids: Sequence[int]) -> str:
    return ",".join(str(i) for i in ids)


def _print_set_root_result(new_root_id: int, result: SetRootResult) -> None:
    previous = result.previous_root_id if result.previous_root_id is not None else "none"
    notified = "yes" if result.notified_previous else "no"
    changed = "yes" if result.changed else "no"
    print(
        f"changed={changed} previous_root={previous} new_root={new_root_id} "
        f"registrations_dropped={len(result.dropped_chat_ids)} "
        f"previous_notified={notified}"
    )


async def _cmd_set_root(user_id: int) -> int:
    services = Services()
    db_path = config.load_db_path()
    async with open_database(db_path) as db:
        async with db.transaction() as c:
            result = await services.set_root(c, user_id)
    _print_set_root_result(user_id, result)
    return 0


async def _cmd_conflicts_list() -> int:
    services = Services()
    db_path = config.load_db_path()
    async with open_database(db_path) as db:
        async with db.reader() as c:
            conflicts = await services.list_conflicts(c, status="open")
    if not conflicts:
        print("no open migration conflicts")
        return 0
    for row in conflicts:
        print(
            f"conflict_id={row.conflict_id} chat_ids=[{_format_ids(row.chat_ids)}] "
            f"reason={row.reason} created_at={row.created_at}"
        )
    return 0


async def _require_open_conflict(
    services: Services, c: aiosqlite.Connection, conflict_id: int
) -> None:
    open_ids = {row.conflict_id for row in await services.list_conflicts(c, status="open")}
    if conflict_id not in open_ids:
        raise CliError(f"conflict {conflict_id} not found or already resolved", code=2)


async def _cmd_conflicts_show(conflict_id: int) -> int:
    services = Services()
    db_path = config.load_db_path()
    async with open_database(db_path) as db:
        async with db.reader() as c:
            await _require_open_conflict(services, c, conflict_id)
            scope = await services.conflict_scope(c, conflict_id)
    print(f"chat_ids=[{_format_ids(scope.chat_ids)}]")
    print(f"alias_pairs=[{','.join(f'{o}->{n}' for o, n in scope.alias_pairs)}]")
    print(f"conflict_ids=[{_format_ids(scope.conflict_ids)}]")
    return 0


def _print_reset_preview(scope: ConflictScope) -> None:
    print("PREVIEW ONLY -- nothing was changed")
    print(f"would resolve conflict_ids=[{_format_ids(scope.conflict_ids)}]")
    print(f"would remove chats=[{_format_ids(scope.chat_ids)}]")
    print(f"would remove alias_pairs=[{','.join(f'{o}->{n}' for o, n in scope.alias_pairs)}]")
    print("roles, users and processed_updates are never touched by this operation")


def _print_reset_applied(result: ResetResult) -> None:
    print("APPLIED -- the scope below was removed")
    print(f"conflict_ids_resolved=[{_format_ids(result.conflict_ids)}]")
    print(f"chat_ids=[{_format_ids(result.chat_ids)}]")
    print(f"chats_removed={result.chats_removed}")
    print(f"subscriptions_removed={result.subscriptions_removed}")
    print(f"events_cancelled={result.events_cancelled}")
    print(f"aliases_removed={result.aliases_removed}")


async def _cmd_conflicts_reset(conflict_id: int, apply: bool) -> int:
    services = Services()
    db_path = config.load_db_path()
    async with open_database(db_path) as db:
        if not apply:
            async with db.reader() as c:
                await _require_open_conflict(services, c, conflict_id)
                scope = await services.conflict_scope(c, conflict_id)
            _print_reset_preview(scope)
            return 0

        async with db.reader() as c:
            await _require_open_conflict(services, c, conflict_id)
        async with db.transaction() as c:
            result = await services.reset_conflict(c, conflict_id)
    _print_reset_applied(result)
    return 0


def _expected_schema_versions() -> list[str]:
    return sorted(path.stem for path in MIGRATIONS_DIR.glob("*.sql"))


def _cmd_backup(destination_path: str, force: bool) -> int:
    """Consistent snapshot via sqlite3.Connection.backup(), safe against a live WAL db.

    Opens the source read-only and lets the SQLite backup API do the copy: it
    walks the source's own pager, so it only ever sees committed data, even if
    another connection holds an open write transaction or has WAL frames not
    yet checkpointed into the main file. A plain file copy of a live WAL
    database would not have that guarantee (plan section 12).
    """
    source_path = config.load_db_path()
    if not Path(source_path).is_file():
        raise CliError(f"source database not found: {source_path}", code=2)

    destination = Path(destination_path)
    if destination.exists() and not force:
        raise CliError(
            f"destination already exists: {destination_path} (use --force to overwrite)",
            code=2,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    tmp_destination = destination.with_name(destination.name + ".tmp")
    if tmp_destination.exists():
        tmp_destination.unlink()

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(tmp_destination))
        try:
            source.backup(dest)
            (page_count,) = dest.execute("PRAGMA page_count").fetchone()
        finally:
            dest.close()
    except sqlite3.Error as exc:
        tmp_destination.unlink(missing_ok=True)
        raise CliError(f"backup failed: sqlite3 error during copy ({exc.__class__.__name__})") from None
    finally:
        source.close()

    os.replace(tmp_destination, destination)
    byte_size = destination.stat().st_size

    print(f"source={source_path}")
    print(f"destination={destination_path}")
    print(f"bytes={byte_size}")
    print(f"pages={page_count}")
    return 0


def _cmd_verify(path: str) -> int:
    """Open a backup read-only and confirm it is a usable, expected-schema database.

    Never writes to `path`. Reports non-secret status lines only (integrity
    check codes and schema version strings), and returns non-zero whenever the
    file is not a usable database or its schema does not match this build's
    migrations.
    """
    target = Path(path)
    if not target.is_file():
        raise CliError(f"backup file not found: {path}", code=1)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError:
            raise CliError(f"not a usable sqlite database: {path}", code=1) from None

        statuses = [row[0] for row in rows]
        if statuses != ["ok"]:
            print(f"integrity_check_failed count={len(statuses)}")
            for status in statuses[:20]:
                print(f"  {status}")
            return 1
        print("integrity_check=ok")

        try:
            cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [row[0] for row in cursor.fetchall()]
        except sqlite3.DatabaseError:
            raise CliError(f"no schema_migrations table: {path}", code=1) from None

        expected = _expected_schema_versions()
        if versions != expected:
            print(f"schema_versions_mismatch expected=[{','.join(expected)}] found=[{','.join(versions)}]")
            return 1
        print(f"schema_versions=[{','.join(versions)}]")
        return 0
    finally:
        conn.close()


def _run_sync(fn: Callable[..., int], *args: object) -> int:
    try:
        return fn(*args)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except Exception as exc:  # unexpected failure: OS error, sqlite error, etc.
        print(f"unexpected failure: {exc}", file=sys.stderr)
        return 1


def _run(coro: Coroutine[object, object, int]) -> int:
    try:
        return asyncio.run(coro)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except Exception as exc:  # unexpected failure: DB error, etc.
        print(f"unexpected failure: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed usage/error to stderr and wrote nothing to
        # the database (we have not touched it yet); normalize its exit code.
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.command == "set-root":
        return _run(_cmd_set_root(args.telegram_user_id))

    if args.command == "migration-conflicts":
        if args.action == "list":
            return _run(_cmd_conflicts_list())
        if args.action == "show":
            return _run(_cmd_conflicts_show(args.conflict_id))
        if args.action == "reset":
            return _run(_cmd_conflicts_reset(args.conflict_id, args.apply))

    if args.command == "backup":
        return _run_sync(_cmd_backup, args.destination_path, args.force)

    if args.command == "verify":
        return _run_sync(_cmd_verify, args.path)

    parser.error("unknown command")  # pragma: no cover - unreachable, subparsers are required
    return 2


if __name__ == "__main__":
    sys.exit(main())
