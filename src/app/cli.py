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
import sys
from typing import Coroutine, Sequence

import aiosqlite

from . import config
from .db import open_database
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

    parser.error("unknown command")  # pragma: no cover - unreachable, subparsers are required
    return 2


if __name__ == "__main__":
    sys.exit(main())
