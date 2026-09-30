"""Table-driven access policy tests: owners, foreign admins, subscribers, nobody."""

import pytest

from app.access import CATALOG, INTERNAL, allowed_commands, can_run, spec
from app.models import Actor, Cmd, Role, Scope

ROOT = Actor(user_id=1, role=Role.ROOT, is_chat_owner=True)
REGISTRAR = Actor(user_id=2, role=Role.ADMIN, is_chat_owner=True)
FOREIGN_ADMIN = Actor(user_id=5, role=Role.ADMIN)
SUBSCRIBER = Actor(user_id=3, is_subscriber=True)
NOBODY = Actor(user_id=4)

ACTORS = {
    "root": ROOT,
    "registrar": REGISTRAR,
    "foreign_admin": FOREIGN_ADMIN,
    "subscriber": SUBSCRIBER,
    "nobody": NOBODY,
}

STAFF = {"root", "registrar", "foreign_admin"}
OWNERS = {"root", "registrar"}
SUB_OR_OWNER = OWNERS | {"subscriber"}

OWNER_ONLY = {Cmd.CHAT_REGISTER, Cmd.CHAT_UNREGISTER, Cmd.LANG}
EVERYONE_ACTIVE = {Cmd.NOTIFY_ON, Cmd.USAGE}
SUB_OR_OWNER_CMDS = {Cmd.NOTIFY_OFF, Cmd.PING, Cmd.LIST, Cmd.RENAME, Cmd.HELP}
PRIVATE_STAFF = {Cmd.P_HELP, Cmd.P_LANG, Cmd.P_USAGE, Cmd.CHAT_LIST}
PRIVATE_ROOT = {Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST, Cmd.CHAT_REMOVE}


def expected(cmd: Cmd, who: str, *, scope: Scope, chat_active: bool) -> bool:
    if spec(cmd).scope is not scope:
        return False
    if scope is Scope.GROUP:
        if not chat_active:
            return cmd in (Cmd.CHAT_REGISTER, Cmd.USAGE, Cmd.HELP) and who in STAFF
        if cmd in OWNER_ONLY:
            return who in OWNERS
        if cmd in EVERYONE_ACTIVE:
            return True
        if cmd in SUB_OR_OWNER_CMDS:
            return who in SUB_OR_OWNER
        raise AssertionError(f"uncovered group cmd {cmd}")
    if cmd in PRIVATE_STAFF:
        return who in STAFF
    if cmd in PRIVATE_ROOT:
        return who == "root"
    raise AssertionError(f"uncovered private cmd {cmd}")


CONTEXTS = [
    ("group_inactive", Scope.GROUP, False),
    ("group_active", Scope.GROUP, True),
    ("private", Scope.PRIVATE, True),
]


@pytest.mark.parametrize("ctx_name,scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("who", list(ACTORS))
@pytest.mark.parametrize("cmd", list(Cmd))
def test_can_run_matches_matrix(cmd, who, ctx_name, scope, chat_active):
    want = expected(cmd, who, scope=scope, chat_active=chat_active)
    got = can_run(cmd, ACTORS[who], scope=scope, chat_active=chat_active)
    assert got is want


@pytest.mark.parametrize("ctx_name,scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("who", list(ACTORS))
def test_allowed_commands_matches_can_run(who, ctx_name, scope, chat_active):
    actor = ACTORS[who]
    got = allowed_commands(actor, scope=scope, chat_active=chat_active)
    want = tuple(
        s
        for s in CATALOG
        if s.scope is scope and can_run(s.cmd, actor, scope=scope, chat_active=chat_active)
    )
    assert got == want


def test_foreign_admin_is_an_ordinary_user_in_an_active_group():
    allowed = allowed_commands(FOREIGN_ADMIN, scope=Scope.GROUP, chat_active=True)
    assert [s.cmd for s in allowed] == [Cmd.NOTIFY_ON, Cmd.USAGE]
    subscribed = Actor(user_id=5, role=Role.ADMIN, is_subscriber=True)
    assert can_run(Cmd.PING, subscribed, scope=Scope.GROUP, chat_active=True)
    assert not can_run(Cmd.CHAT_UNREGISTER, subscribed, scope=Scope.GROUP, chat_active=True)


def test_inactive_group_offers_staff_only_register_help_and_usage():
    for actor in (ROOT, REGISTRAR, FOREIGN_ADMIN):
        cmds = [s.cmd for s in allowed_commands(actor, scope=Scope.GROUP, chat_active=False)]
        assert cmds == [Cmd.CHAT_REGISTER, Cmd.HELP, Cmd.USAGE]
    for actor in (SUBSCRIBER, NOBODY):
        assert allowed_commands(actor, scope=Scope.GROUP, chat_active=False) == ()


def test_private_scope_ignores_subscription_and_ownership_of_ordinary_users():
    assert allowed_commands(SUBSCRIBER, scope=Scope.PRIVATE, chat_active=True) == ()
    assert allowed_commands(NOBODY, scope=Scope.PRIVATE, chat_active=True) == ()


def test_wrong_scope_command_never_runs():
    assert not can_run(Cmd.ADMIN_LIST, ROOT, scope=Scope.GROUP, chat_active=True)
    assert not can_run(Cmd.PING, ROOT, scope=Scope.PRIVATE, chat_active=True)


def test_catalog_covers_every_cmd_exactly_once():
    cmds = [s.cmd for s in CATALOG]
    assert sorted(cmds, key=lambda c: c.value) == sorted(set(Cmd), key=lambda c: c.value)
    assert len(cmds) == len(set(cmds))


def test_internal_entries_are_the_two_usage_cmds():
    assert INTERNAL == {Cmd.USAGE, Cmd.P_USAGE}
