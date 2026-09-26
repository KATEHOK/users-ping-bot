"""Table-driven access policy tests (plan section 15 acceptance criterion)."""

import pytest

from app.access import CATALOG, allowed_commands, can_run, spec
from app.models import Actor, Cmd, Role, Scope

ROOT = Actor(user_id=1, role=Role.ROOT, is_subscriber=False)
ADMIN = Actor(user_id=2, role=Role.ADMIN, is_subscriber=False)
SUBSCRIBER = Actor(user_id=3, role=None, is_subscriber=True)
NOBODY = Actor(user_id=4, role=None, is_subscriber=False)

ACTORS = {"root": ROOT, "admin": ADMIN, "subscriber": SUBSCRIBER, "nobody": NOBODY}

GROUP_STAFF_ONLY = {Cmd.CHAT_REGISTER, Cmd.CHAT_UNREGISTER}
GROUP_SUB_OR_STAFF = {Cmd.NOTIFY_OFF, Cmd.PING, Cmd.LIST, Cmd.HELP}
PRIVATE_STAFF_ONLY = {Cmd.P_HELP, Cmd.CHAT_LIST}
PRIVATE_ROOT_ONLY = {Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST, Cmd.CHAT_REMOVE}


def expected(cmd: Cmd, actor_label: str, *, scope: Scope, chat_active: bool) -> bool:
    s = spec(cmd)
    if s.scope is not scope:
        return False  # wrong scope entirely: never runnable

    is_staff = actor_label in ("root", "admin")
    is_root = actor_label == "root"
    is_subscriber = actor_label == "subscriber"

    if scope is Scope.GROUP:
        if not chat_active:
            return cmd is Cmd.CHAT_REGISTER and is_staff
        if cmd in GROUP_STAFF_ONLY:
            return is_staff
        if cmd is Cmd.NOTIFY_ON:
            return True
        if cmd in GROUP_SUB_OR_STAFF:
            return is_staff or is_subscriber
        raise AssertionError(f"uncovered group cmd {cmd}")

    # PRIVATE
    if cmd in PRIVATE_STAFF_ONLY:
        return is_staff
    if cmd in PRIVATE_ROOT_ONLY:
        return is_root
    raise AssertionError(f"uncovered private cmd {cmd}")


CONTEXTS = [
    ("group_inactive", Scope.GROUP, False),
    ("group_active", Scope.GROUP, True),
    ("private", Scope.PRIVATE, True),  # chat_active is meaningless in PRIVATE scope
]


@pytest.mark.parametrize("ctx_name,scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("actor_label", list(ACTORS))
@pytest.mark.parametrize("cmd", list(Cmd))
def test_can_run_matches_matrix(cmd, actor_label, ctx_name, scope, chat_active):
    actor = ACTORS[actor_label]
    want = expected(cmd, actor_label, scope=scope, chat_active=chat_active)
    got = can_run(cmd, actor, scope=scope, chat_active=chat_active)
    assert got is want, (
        f"can_run({cmd}, {actor_label}, scope={scope}, chat_active={chat_active}) "
        f"= {got}, expected {want}"
    )


def test_can_run_never_raises_for_any_catalog_cmd():
    for s in CATALOG:
        for scope, chat_active in ((Scope.GROUP, False), (Scope.GROUP, True), (Scope.PRIVATE, True)):
            for actor in ACTORS.values():
                can_run(s.cmd, actor, scope=scope, chat_active=chat_active)  # must not raise


@pytest.mark.parametrize("ctx_name,scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("actor_label", list(ACTORS))
def test_allowed_commands_matches_can_run(actor_label, ctx_name, scope, chat_active):
    actor = ACTORS[actor_label]
    got = allowed_commands(actor, scope=scope, chat_active=chat_active)
    want = tuple(
        s
        for s in CATALOG
        if s.scope is scope and can_run(s.cmd, actor, scope=scope, chat_active=chat_active)
    )
    assert got == want
    assert all(s.scope is scope for s in got)


def test_unregistered_group_root_gets_only_register():
    for actor in (ROOT, ADMIN):
        allowed = allowed_commands(actor, scope=Scope.GROUP, chat_active=False)
        assert [s.cmd for s in allowed] == [Cmd.CHAT_REGISTER]
    for actor in (SUBSCRIBER, NOBODY):
        allowed = allowed_commands(actor, scope=Scope.GROUP, chat_active=False)
        assert allowed == ()


def test_notify_on_is_the_only_no_role_group_command():
    allowed = allowed_commands(NOBODY, scope=Scope.GROUP, chat_active=True)
    assert [s.cmd for s in allowed] == [Cmd.NOTIFY_ON]


def test_private_scope_never_grants_from_group_subscription():
    subscriber_admin_elsewhere = Actor(user_id=5, role=None, is_subscriber=True)
    assert allowed_commands(subscriber_admin_elsewhere, scope=Scope.PRIVATE, chat_active=True) == ()


def test_role_alone_never_implies_subscription():
    root_not_subscribed = Actor(user_id=1, role=Role.ROOT, is_subscriber=False)
    # root still passes because it is staff, not because of subscription -- check the
    # narrower subscriber-or-staff commands really key off is_staff, not is_subscriber.
    assert can_run(Cmd.PING, root_not_subscribed, scope=Scope.GROUP, chat_active=True) is True
    admin_not_subscribed = Actor(user_id=2, role=Role.ADMIN, is_subscriber=False)
    assert can_run(Cmd.LIST, admin_not_subscribed, scope=Scope.GROUP, chat_active=True) is True


def test_catalog_covers_every_cmd_exactly_once():
    cmds = [s.cmd for s in CATALOG]
    assert sorted(cmds, key=lambda c: c.value) == sorted(set(Cmd), key=lambda c: c.value)
    assert len(cmds) == len(set(cmds))
