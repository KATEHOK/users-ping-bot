import re

import pytest

from app import access, rendering
from app.models import Actor, Cmd, Role, Scope, SubscriberRef
from app.rendering import (
    AdminListRow,
    ChatListRow,
    esc,
    farewell_text,
    help_text,
    mention,
    root_revoked_text,
    split_mentions,
    split_text,
    welcome_text,
)


def test_esc_escapes_html_and_keeps_unicode():
    raw = "<b>bold</b> & \"quoted\" — Алиса"
    out = esc(raw)
    assert "<b>" not in out
    assert "&lt;b&gt;" in out
    assert "&amp;" in out
    assert "Алиса" in out  # unicode preserved, not mangled


def test_mention_always_uses_id_link_even_with_username():
    m = mention(555, "Some Name", "someusername")
    assert m == '<a href="tg://user?id=555">Some Name</a>'
    assert "someusername" not in m
    assert "@" not in m


def test_mention_falls_back_to_id_label_when_no_display_name():
    m = mention(777, None, "someusername")
    assert m == '<a href="tg://user?id=777">id777</a>'
    assert "someusername" not in m


def test_mention_escapes_html_injection_in_display_name():
    m = mention(1, '<script>alert(1)</script>', None)
    assert "<script>" not in m
    assert "&lt;script&gt;" in m
    assert m.startswith('<a href="tg://user?id=1">')


def test_split_mentions_never_splits_a_single_mention_and_round_trips_ids():
    ids = list(range(1, 201))
    parts = [mention(i, f"User {i}", None) for i in ids]
    chunks = split_mentions(parts, limit=120)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 120 or chunk.count("<a href") == 1  # oversized singles allowed alone
    found_ids = [int(x) for c in chunks for x in re.findall(r'tg://user\?id=(\d+)', c)]
    assert sorted(found_ids) == ids  # nobody lost, nobody duplicated
    # never split inside one mention: every chunk must have balanced anchor tags
    for chunk in chunks:
        assert chunk.count("<a ") == chunk.count("</a>")


def test_split_mentions_oversized_single_mention_goes_out_alone():
    huge_name = "x" * 200
    huge = mention(1, huge_name, None)
    normal = [mention(i, f"u{i}", None) for i in range(2, 5)]
    parts = [huge] + normal
    chunks = split_mentions(parts, limit=50)
    assert huge in chunks
    huge_chunk = next(c for c in chunks if huge in c)
    assert huge_chunk == huge  # alone, not merged with anything else


def test_split_mentions_empty_input():
    assert split_mentions([], limit=100) == []


def test_split_text_splits_on_newlines_under_limit():
    text = "\n".join(f"line{i}" for i in range(50))
    chunks = split_text(text, limit=30)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 30
    # reassembled lines match original, in order, none lost
    assert "\n".join(chunks).split("\n") == text.split("\n")


def test_welcome_and_farewell_and_root_revoked_are_nonempty_english():
    assert "notify on" in welcome_text()
    assert "unregistered" in farewell_text().lower()
    assert "2026" in root_revoked_text("2026-09-26T12:00:00+00:00")


ROOT = Actor(user_id=1, role=Role.ROOT, is_subscriber=False)
ADMIN = Actor(user_id=2, role=Role.ADMIN, is_subscriber=False)
SUBSCRIBER = Actor(user_id=3, role=None, is_subscriber=True)
NOBODY = Actor(user_id=4, role=None, is_subscriber=False)

CONTEXTS = [
    (Scope.GROUP, False),
    (Scope.GROUP, True),
    (Scope.PRIVATE, True),
]


@pytest.mark.parametrize("scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("actor", [ROOT, ADMIN, SUBSCRIBER, NOBODY])
def test_help_text_lists_exactly_the_allowed_commands(actor, scope, chat_active):
    allowed = access.allowed_commands(actor, scope=scope, chat_active=chat_active)
    text = help_text(actor, scope=scope, chat_active=chat_active)
    allowed_lines = {f"{s.syntax} - {s.summary}" for s in allowed}
    disallowed_lines = {
        f"{s.syntax} - {s.summary}" for s in access.CATALOG
    } - allowed_lines
    for line in allowed_lines:
        assert line in text
    for line in disallowed_lines:
        assert line not in text


def test_subscriber_help_has_no_administrative_commands():
    text = help_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True)
    for admin_cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST, Cmd.CHAT_REMOVE):
        assert access.spec(admin_cmd).syntax not in text


def test_group_help_never_lists_admin_management_even_for_root():
    text = help_text(ROOT, scope=Scope.GROUP, chat_active=True)
    for admin_cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST):
        assert access.spec(admin_cmd).syntax not in text


def test_unregistered_group_help_only_offers_register_even_for_root():
    # root gets no help/list/ping in an inactive group -- only CHAT_REGISTER is allowed
    allowed = access.allowed_commands(ROOT, scope=Scope.GROUP, chat_active=False)
    assert [s.cmd for s in allowed] == [Cmd.CHAT_REGISTER]
    text = help_text(ROOT, scope=Scope.GROUP, chat_active=False)
    assert access.spec(Cmd.HELP).syntax not in text
    assert access.spec(Cmd.LIST).syntax not in text
    assert access.spec(Cmd.PING).syntax not in text
    assert access.spec(Cmd.CHAT_REGISTER).syntax in text


def test_private_help_mentions_dm_role_management_for_root():
    text = help_text(ROOT, scope=Scope.PRIVATE, chat_active=True)
    assert "/admin" in text


def test_ping_help_contains_delivery_note_when_ping_allowed():
    text = help_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True)
    assert "fixed" in text.lower()
    assert "not guaranteed" in text.lower()


def test_ping_help_omits_delivery_note_when_ping_not_allowed():
    text = help_text(NOBODY, scope=Scope.GROUP, chat_active=True)
    assert "not guaranteed" not in text.lower()


def test_subscriber_list_text_is_plain_no_links_and_round_trips_ids():
    subs = [
        SubscriberRef(user_id=i, subscription_id=i, display_name=f"Name {i}", username=None)
        for i in range(1, 60)
    ]
    chunks = rendering.subscriber_list_text(subs)
    joined = "\n".join(chunks)
    assert "tg://" not in joined
    assert "<a " not in joined
    found_ids = sorted(int(x) for x in re.findall(r"- (\d+)$", joined, re.MULTILINE))
    assert found_ids == list(range(1, 60))


def test_subscriber_list_text_empty_is_empty_list():
    assert rendering.subscriber_list_text([]) == []


def test_subscriber_list_text_html_injection_escaped():
    subs = [SubscriberRef(user_id=1, subscription_id=1, display_name="<b>x</b>", username=None)]
    text = "\n".join(rendering.subscriber_list_text(subs))
    assert "<b>" not in text
    assert "&lt;b&gt;" in text


def test_subscriber_list_text_unknown_name_falls_back():
    subs = [SubscriberRef(user_id=9, subscription_id=1, display_name=None, username=None)]
    text = "\n".join(rendering.subscriber_list_text(subs))
    assert "name unknown" in text


def test_chat_list_text_shows_blocked_and_unknown_name():
    rows = [
        ChatListRow(chat_id=-100111, title="Team Chat", registered_by=42, blocked=False),
        ChatListRow(chat_id=-100222, title=None, registered_by=43, blocked=True),
    ]
    text = "\n".join(rendering.chat_list_text(rows))
    assert "Team Chat" in text
    assert "-100111" in text
    assert "registrar=42" in text
    assert "name unknown" in text
    assert "BLOCKED" in text


def test_chat_list_text_empty():
    assert rendering.chat_list_text([]) == []


def test_admin_list_text_plain_no_links():
    rows = [
        AdminListRow(user_id=10, display_name="Ann", username="ann_tg"),
        AdminListRow(user_id=11, display_name=None, username=None),
    ]
    text = "\n".join(rendering.admin_list_text(rows))
    assert "Ann" in text
    assert "10" in text
    assert "name unknown" in text
    assert "tg://" not in text


def test_admin_list_text_empty():
    assert rendering.admin_list_text([]) == []


def test_pong_constant():
    assert rendering.PONG == "pong"
