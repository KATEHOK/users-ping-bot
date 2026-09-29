import re
from types import SimpleNamespace

import pytest
from conftest import find_html_errors

from app import access, rendering
from app.models import LANGS, Actor, Cmd, Role, Scope, SubscriberRef
from app.rendering import (
    MAX_MENTIONS,
    esc,
    farewell_text,
    help_text,
    mention,
    root_revoked_text,
    split_mentions,
    split_text,
    t,
    usage_text,
    welcome_text,
)

ROOT = Actor(user_id=1, role=Role.ROOT, is_chat_owner=True)
REGISTRAR = Actor(user_id=2, role=Role.ADMIN, is_chat_owner=True)
FOREIGN_ADMIN = Actor(user_id=5, role=Role.ADMIN)
SUBSCRIBER = Actor(user_id=3, is_subscriber=True)
NOBODY = Actor(user_id=4)
ACTORS = [ROOT, REGISTRAR, FOREIGN_ADMIN, SUBSCRIBER, NOBODY]

CONTEXTS = [(Scope.GROUP, False), (Scope.GROUP, True), (Scope.PRIVATE, True)]

EVIL = '<b>x</b> & "q" <script>'


def test_html_validator_sanity():
    assert find_html_errors('<a href="tg://user?id=1">x &amp; y</a> <b>ok</b>') == []
    assert find_html_errors("use /admin create <user_id>")
    assert find_html_errors("a & b")
    assert find_html_errors("1 > 0")
    assert find_html_errors("<div>x</div>")
    assert find_html_errors("<b>x")
    assert find_html_errors('<b class="x">x</b>')


# --- catalogue ---


def test_every_key_has_both_languages_and_no_placeholder_drift():
    assert set(LANGS) == {"en", "ru"}
    for key in rendering._CATALOG:
        fields = {}
        for lang in LANGS:
            template = rendering._CATALOG[key][0 if lang == "en" else 1]
            assert template.strip(), (key, lang)
            fields[lang] = set(re.findall(r"{(\w+)}", template))
        assert fields["en"] == fields["ru"], key


def test_every_listed_command_has_a_description_in_both_languages():
    for s in access.CATALOG:
        if s.cmd not in access.INTERNAL:
            assert "cmd_" + s.cmd.value in rendering._CATALOG
            for lang in LANGS:
                assert t("cmd_" + s.cmd.value, lang).strip()


def test_command_spec_has_no_duplicate_description():
    assert not hasattr(access.CommandSpec, "summary")


def test_decision_keys_present():
    for key in (
        "welcome already_registered subscribed already_subscribed unsubscribed not_subscribed "
        "list_empty farewell lang_set root_cli_only bad_args admin_created admin_exists "
        "admin_removed admin_absent chat_removed chat_absent root_revoked startup name_unknown "
        "help_register_hint"
    ).split():
        assert key in rendering._CATALOG


def test_t_escapes_keyword_values_and_resolves_language():
    assert t("bad_args", "en", syntax="/admin create <user_id>") == (
        "Invalid arguments. Usage: <code>/admin create &lt;user_id&gt;</code>"
    )
    assert "&lt;" in t("bad_args", "ru", syntax="<x>")
    assert t("lang_set", "en") == "Language: English."
    assert t("lang_set", "ru") != t("lang_set", "en")
    assert "&amp;" in t("admin_created", "en", id="a&b")
    assert t("admin_removed", "en", id=5, n=2) == "Admin revoked: 5. Chats removed: 2."


def test_russian_texts_are_gender_and_number_neutral():
    banned = re.compile(r"(?i)\b(\u0432\u044b|\u0442\u044b|\u0432\u0430\u0448\w*|\u0442\u0435\u0431\u044f|\u0432\u0430\u043c)\b")
    for key in rendering._CATALOG:
        assert not banned.search(rendering._CATALOG[key][1]), key


# --- escaping and mentions ---


def test_esc_escapes_html_and_keeps_unicode():
    out = esc("<b>bold</b> & \u0410\u043b\u0438\u0441\u0430")
    assert "<b>" not in out and "&lt;b&gt;" in out and "&amp;" in out
    assert "\u0410\u043b\u0438\u0441\u0430" in out


def test_mention_is_an_id_link_never_an_at_username():
    m = mention(555, "Some Name")
    assert m == '<a href="tg://user?id=555">Some Name</a>'


def test_mention_falls_back_to_id_label_when_no_display_name():
    assert mention(777, None) == '<a href="tg://user?id=777">id777</a>'


def test_mention_escapes_html_injection_in_display_name():
    m = mention(1, EVIL)
    assert find_html_errors(m) == []
    assert "<script>" not in m


# --- splitting ---


def test_split_mentions_caps_at_50_without_loss_or_duplication():
    ids = list(range(1, 173))
    parts = [mention(i, f"U{i}") for i in ids]
    chunks = split_mentions(parts)
    assert [c.count("<a ") for c in chunks] == [50, 50, 50, 22]
    assert max(c.count("<a ") for c in chunks) <= MAX_MENTIONS
    found = [int(x) for c in chunks for x in re.findall(r"tg://user\?id=(\d+)", c)]
    assert found == ids
    for c in chunks:
        assert find_html_errors(c) == []


def test_split_mentions_respects_char_limit_and_never_splits_one():
    ids = list(range(1, 201))
    parts = [mention(i, f"User {i}") for i in ids]
    chunks = split_mentions(parts, limit=120)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 120 or c.count("<a href") == 1
        assert c.count("<a ") == c.count("</a>")
    found = [int(x) for c in chunks for x in re.findall(r"tg://user\?id=(\d+)", c)]
    assert found == ids


def test_split_mentions_default_char_limit():
    parts = [mention(i, "n" * 200) for i in range(1, 60)]
    chunks = split_mentions(parts)
    assert all(len(c) <= rendering.MAX_MESSAGE for c in chunks)
    assert sum(c.count("<a ") for c in chunks) == 59


def test_split_mentions_oversized_single_mention_goes_out_alone():
    huge = mention(1, "x" * 200)
    chunks = split_mentions([huge] + [mention(i, f"u{i}") for i in range(2, 5)], limit=50)
    assert huge in chunks


def test_split_mentions_empty_and_custom_max_items():
    assert split_mentions([], limit=100) == []
    assert split_mentions(["a", "b", "c"], max_items=2) == ["a b", "c"]


def test_split_text_splits_on_newlines_under_limit():
    text = "\n".join(f"line{i}" for i in range(50))
    chunks = split_text(text, limit=30)
    assert len(chunks) > 1 and all(len(c) <= 30 for c in chunks)
    assert "\n".join(chunks).split("\n") == text.split("\n")


# --- single texts ---


@pytest.mark.parametrize("lang", LANGS)
def test_simple_texts(lang):
    assert welcome_text(lang) == t("welcome", lang)
    assert farewell_text(lang) == t("farewell", lang)
    assert "2026" in root_revoked_text("2026-09-26T12:00:00+00:00", lang)
    assert "/on" in welcome_text(lang) and "<code>" not in welcome_text(lang)


def test_pong_constant():
    assert rendering.PONG == "pong"


# --- help / usage ---


def _syntax_lines(text: str) -> set[str]:
    # "/alias \u2014 description" -> "/alias"; a "<pre>SYNTAX</pre>" line -> "SYNTAX" (still escaped)
    shown = set()
    for line in text.split("\n"):
        if line.startswith("<pre>"):
            shown.add(line.removeprefix("<pre>").removesuffix("</pre>"))
        elif line.startswith("/"):
            shown.add(line.split(" \u2014 ")[0])
    return shown


def _shown(s: access.CommandSpec) -> str:
    return esc(s.alias or s.syntax)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("actor", ACTORS)
def test_help_lists_exactly_the_allowed_commands(actor, scope, chat_active, lang):
    allowed = access.allowed_commands(actor, scope=scope, chat_active=chat_active)
    listed = {
        _shown(s)
        for s in allowed
        if s.cmd not in access.INTERNAL and not (chat_active and s.cmd is Cmd.CHAT_REGISTER)
    }
    text = help_text(actor, scope=scope, chat_active=chat_active, lang=lang)
    assert _syntax_lines(text) == listed
    assert find_html_errors(text) == []


@pytest.mark.parametrize("scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("actor", ACTORS)
def test_help_never_lists_internal_entries(actor, scope, chat_active):
    text = help_text(actor, scope=scope, chat_active=chat_active, lang="en")
    for cmd in access.INTERNAL:
        assert esc(access.spec(cmd).syntax) not in text
    assert "/admin, /chat" not in text


def test_group_help_shows_only_the_alias_as_plain_text():
    for lang in LANGS:
        text = help_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, lang=lang)
        for alias in ("/all", "/on", "/off", "/help", "/list"):
            assert any(line.startswith(f"{alias} \u2014 ") for line in text.split("\n"))
        assert "/upb" not in text and "<code>" not in text and "<pre>" not in text
    private = help_text(ROOT, scope=Scope.PRIVATE, chat_active=True, lang="en")
    assert "\n/help \u2014 Help\n" in private


def test_help_texts_en_and_ru_for_owner_member_and_private_root():
    owner = Actor(user_id=2, role=Role.ADMIN, is_chat_owner=True)
    en = {
        "owner": help_text(owner, scope=Scope.GROUP, chat_active=True, lang="en"),
        "member": help_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, lang="en"),
    }
    assert en["owner"] == (
        "Commands:\n/on \u2014 Subscribe\n/off \u2014 Unsubscribe\n/all \u2014 Ping all\n"
        "/list \u2014 Subscribers\n/help \u2014 Help\n/unregister \u2014 Unregister\n"
        "/lang &lt;en|ru&gt; \u2014 Language"
    )
    assert en["member"] == (
        "Commands:\n/on \u2014 Subscribe\n/off \u2014 Unsubscribe\n/all \u2014 Ping all\n"
        "/list \u2014 Subscribers\n/help \u2014 Help"
    )
    ru_owner = help_text(owner, scope=Scope.GROUP, chat_active=True, lang="ru")
    assert ru_owner == (
        "\u041a\u043e\u043c\u0430\u043d\u0434\u044b:\n/on \u2014 \u041f\u043e\u0434\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f\n"
        "/off \u2014 \u041e\u0442\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f\n/all \u2014 \u041f\u043e\u0437\u0432\u0430\u0442\u044c \u0432\u0441\u0435\u0445\n"
        "/list \u2014 \u041f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u043a\u0438\n/help \u2014 \u0421\u043f\u0440\u0430\u0432\u043a\u0430\n"
        "/unregister \u2014 \u0421\u043d\u044f\u0442\u044c \u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044e\n/lang &lt;en|ru&gt; \u2014 \u042f\u0437\u044b\u043a"
    )
    ru_member = help_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, lang="ru")
    assert "/unregister" not in ru_member and "/lang" not in ru_member
    assert ru_member.split("\n")[1:] == ru_owner.split("\n")[1:6]
    assert help_text(ROOT, scope=Scope.PRIVATE, chat_active=True, lang="en") == (
        "Commands:\n/help \u2014 Help\n"
        "Language\n<pre>/lang &lt;en|ru&gt;</pre>\n"
        "Grant admin\n<pre>/admin create &lt;user_id&gt;</pre>\n"
        "Revoke admin\n<pre>/admin remove &lt;user_id&gt;</pre>\n"
        "Admins\n<pre>/admin list</pre>\n"
        "Chats\n<pre>/chat list</pre>\n"
        "Remove chat\n<pre>/chat remove &lt;chat_id&gt;</pre>\n" + t("help_register_hint", "en")
    )
    ru_private = help_text(ROOT, scope=Scope.PRIVATE, chat_active=True, lang="ru")
    assert "\n\u042f\u0437\u044b\u043a\n<pre>/lang &lt;en|ru&gt;</pre>\n" in ru_private
    assert "\n\u0427\u0430\u0442\u044b\n<pre>/chat list</pre>\n" in ru_private


@pytest.mark.parametrize("lang", LANGS)
def test_help_and_usage_have_no_ping_note_and_no_code_around_commands(lang):
    assert "ping_note" not in rendering._CATALOG
    for actor in ACTORS:
        for scope, active in CONTEXTS:
            texts = [help_text(actor, scope=scope, chat_active=active, lang=lang)]
            texts.append(usage_text(actor, scope=scope, chat_active=active, prefix=(), lang=lang))
            for text in texts:
                assert "Delivery is not guaranteed" not in text
                assert "\u0414\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u043d\u0435 \u0433\u0430\u0440\u0430\u043d\u0442\u0438\u0440\u0443\u0435\u0442\u0441\u044f" not in text
                assert "<code>" not in text.replace(t("help_register_hint", lang), "")


def test_pre_block_only_for_multiword_command_or_description():
    for lang in LANGS:
        text = help_text(ROOT, scope=Scope.PRIVATE, chat_active=True, lang=lang)
        lines = text.split("\n")
        for s in access.allowed_commands(ROOT, scope=Scope.PRIVATE, chat_active=True):
            if s.cmd in access.INTERNAL:
                continue
            desc = t("cmd_" + s.cmd.value, lang)
            multi = len(s.syntax.split()) >= 2 or len(desc.split()) >= 2
            block = f"<pre>{esc(s.syntax)}</pre>"
            if multi:
                i = lines.index(block)
                assert lines[i - 1] == desc
            else:
                assert block not in lines
                assert f"{esc(s.syntax)} \u2014 {desc}" in lines


def test_descriptions_are_short():
    for key, (en, ru) in rendering._CATALOG.items():
        if key.startswith("cmd_"):
            assert len(en.split()) <= 2 and len(ru.split()) <= 2, key


def test_usage_shows_alias_and_prefix_matching_ignores_it():
    text = usage_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, prefix=("notify",), lang="en")
    assert _syntax_lines(text) == {"/on", "/off", "/list"}
    text = usage_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, prefix=("all",), lang="en")
    assert "/all \u2014 Ping all" in text  # unknown prefix: everything allowed


def test_help_escapes_command_syntax():
    text = help_text(ROOT, scope=Scope.PRIVATE, chat_active=True, lang="en")
    assert "<pre>/admin create &lt;user_id&gt;</pre>" in text
    assert "<user_id>" not in text
    assert "<pre>/lang &lt;en|ru&gt;</pre>" in text
    group = help_text(REGISTRAR, scope=Scope.GROUP, chat_active=True, lang="en")
    assert "/lang &lt;en|ru&gt; \u2014 Language" in group and "<en|ru>" not in group


def test_private_help_carries_group_registration_hint_in_both_languages():
    for lang in LANGS:
        text = help_text(REGISTRAR, scope=Scope.PRIVATE, chat_active=True, lang=lang)
        assert t("help_register_hint", lang) in text
        assert "<code>/register</code>" in text
    group = help_text(REGISTRAR, scope=Scope.GROUP, chat_active=True, lang="en")
    assert t("help_register_hint", "en") not in group


def test_subscriber_help_has_no_administrative_commands():
    text = help_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, lang="en")
    for cmd in (Cmd.ADMIN_CREATE, Cmd.ADMIN_REMOVE, Cmd.ADMIN_LIST, Cmd.CHAT_REMOVE):
        assert esc(access.spec(cmd).syntax) not in text


def test_usage_filters_by_prefix():
    text = usage_text(REGISTRAR, scope=Scope.GROUP, chat_active=True, prefix=("chat",), lang="en")
    assert _syntax_lines(text) == {"/unregister"}  # register is not advertised when active
    text = usage_text(FOREIGN_ADMIN, scope=Scope.GROUP, chat_active=False, prefix=("chat",), lang="en")
    assert _syntax_lines(text) == {"/register"}
    text = usage_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, prefix=("notify",), lang="en")
    assert _syntax_lines(text) == {"/on", "/off", "/list"}
    text = usage_text(REGISTRAR, scope=Scope.GROUP, chat_active=True, prefix=("lang",), lang="en")
    assert _syntax_lines(text) == {"/lang &lt;en|ru&gt;"}
    text = usage_text(ROOT, scope=Scope.PRIVATE, chat_active=True, prefix=("admin",), lang="en")
    assert _syntax_lines(text) == {
        "/admin create &lt;user_id&gt;",
        "/admin remove &lt;user_id&gt;",
        "/admin list",
    }
    text = usage_text(ROOT, scope=Scope.PRIVATE, chat_active=True, prefix=("lang",), lang="ru")
    assert _syntax_lines(text) == {"/lang &lt;en|ru&gt;"}


def test_usage_examples_per_actor_and_scope():
    def lines(actor, scope, active, prefix):
        return _syntax_lines(usage_text(actor, scope=scope, chat_active=active, prefix=prefix, lang="en"))

    plain = Actor(user_id=7)
    # bare or unknown: everything allowed
    assert lines(plain, Scope.GROUP, True, ()) == {"/on"}
    assert lines(plain, Scope.GROUP, True, ("qwe",)) == {"/on"}
    assert lines(FOREIGN_ADMIN, Scope.GROUP, False, ()) == {"/register", "/help"}
    assert lines(FOREIGN_ADMIN, Scope.GROUP, False, ("qwe",)) == {"/register", "/help"}
    # known prefix with nothing allowed under it: silence, no fallback
    assert usage_text(plain, scope=Scope.GROUP, chat_active=True, prefix=("chat",), lang="en") == ""
    assert usage_text(plain, scope=Scope.GROUP, chat_active=True, prefix=("lang",), lang="en") == ""
    assert usage_text(FOREIGN_ADMIN, scope=Scope.GROUP, chat_active=False, prefix=("notify",), lang="en") == ""
    assert usage_text(FOREIGN_ADMIN, scope=Scope.PRIVATE, chat_active=True, prefix=("admin",), lang="en") == ""
    assert lines(FOREIGN_ADMIN, Scope.PRIVATE, True, ("chat",)) == {"/chat list"}
    assert lines(FOREIGN_ADMIN, Scope.PRIVATE, True, ("lang",)) == {"/lang &lt;en|ru&gt;"}


@pytest.mark.parametrize("scope,chat_active", CONTEXTS)
@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("prefix", [(), ("qwe",), ("chat",), ("notify",), ("admin",), ("lang",)])
def test_usage_is_empty_only_when_nothing_matches(actor, scope, chat_active, prefix):
    text = usage_text(actor, scope=scope, chat_active=chat_active, prefix=prefix, lang="ru")
    allowed = [
        s for s in access.allowed_commands(actor, scope=scope, chat_active=chat_active)
        if s.cmd not in access.INTERNAL
    ]
    if not prefix or prefix[0] == "qwe":
        assert (text == "") == (not allowed)
    if text:
        assert find_html_errors(text) == []
        assert _syntax_lines(text) <= {_shown(s) for s in allowed}


def test_usage_never_falls_back_for_known_prefix():
    text = usage_text(SUBSCRIBER, scope=Scope.GROUP, chat_active=True, prefix=("chat",), lang="en")
    assert text == ""


# --- lists ---


def test_subscriber_list_has_no_links_no_at_and_round_trips_ids():
    subs = [
        SubscriberRef(user_id=i, display_name=f"Name {i}", username=f"user{i}")
        for i in range(1, 60)
    ]
    subs.append(SubscriberRef(user_id=100, display_name="@admin", username="@evil"))
    joined = "\n".join(rendering.subscriber_list_text(subs, "en"))
    assert "tg://" not in joined and "<a " not in joined and "@" not in joined
    assert "user7" in joined and "Name 7" in joined
    found = sorted(int(x) for x in re.findall(r" - (\d+)$", joined, re.MULTILINE))
    assert found == [*range(1, 60), 100]


def test_subscriber_list_empty_and_unknown_name():
    assert rendering.subscriber_list_text([], "en") == []
    text = "\n".join(rendering.subscriber_list_text([SubscriberRef(user_id=9)], "ru"))
    assert t("name_unknown", "ru") in text and "9" in text


def test_lists_escape_html_in_names():
    sub = SubscriberRef(user_id=1, display_name=EVIL, username=None)
    chunks = rendering.subscriber_list_text([sub], "en")
    assert all(find_html_errors(c) == [] for c in chunks)
    row = SimpleNamespace(chat_id=-100, title=EVIL, registered_by=1)
    assert "<script>" not in "\n".join(rendering.chat_list_text([row], "en", show_registrar=True))
    adm = SimpleNamespace(user_id=1, display_name=EVIL, username="u")
    assert "<script>" not in "\n".join(rendering.admin_list_text([adm], "en"))


def test_chat_list_registrar_visibility_and_unknown_title():
    rows = [
        SimpleNamespace(chat_id=-100111, title="Team Chat", registered_by=42),
        SimpleNamespace(chat_id=-100222, title=None, registered_by=43),
    ]
    own = "\n".join(rendering.chat_list_text(rows, "en", show_registrar=False))
    assert "Team Chat" in own and "-100111" in own and "42" not in own
    assert "name unknown" in own
    allr = "\n".join(rendering.chat_list_text(rows, "ru", show_registrar=True))
    assert "42" in allr and "43" in allr and t("name_unknown", "ru") in allr
    assert "tg://" not in allr and "@" not in allr
    assert rendering.chat_list_text([], "en", show_registrar=True) == []


def test_admin_list_plain_no_links():
    rows = [
        SimpleNamespace(user_id=10, display_name="Ann", username="ann_tg"),
        SimpleNamespace(user_id=11, display_name=None, username=None),
    ]
    text = "\n".join(rendering.admin_list_text(rows, "en"))
    assert "Ann" in text and "10" in text and "name unknown" in text
    assert "tg://" not in text and "@" not in text
    assert rendering.admin_list_text([], "en") == []


def test_startup_report():
    rows = [SimpleNamespace(chat_id=-100111, title="T", registered_by=42)]
    text = "\n".join(rendering.startup_report_text([-1, -2], rows, "en"))
    lines = text.split("\n")
    assert lines[0] == t("startup", "en", n=2)
    assert "-1, -2" in lines[1]
    assert lines[-1] == "T | -100111 | 42"
    quiet = rendering.startup_report_text([], [], "ru")
    assert quiet == [t("startup", "ru", n=0)]


# --- every rendered text is valid Telegram HTML ---


@pytest.mark.parametrize("lang", LANGS)
def test_all_rendered_texts_pass_the_html_validator(lang):
    texts: list[str] = [
        t(key, lang, syntax="/x <a|b>", id="<1>", n=1, chat_id=-1, time="<t>", ids="1, 2")
        for key in rendering._CATALOG
    ]
    texts += [welcome_text(lang), farewell_text(lang), root_revoked_text("<now>", lang)]
    for actor in ACTORS:
        for scope, active in CONTEXTS:
            texts.append(help_text(actor, scope=scope, chat_active=active, lang=lang))
            for prefix in ((), ("chat",), ("notify",), ("admin",), ("lang",), ("zzz",)):
                texts.append(
                    usage_text(actor, scope=scope, chat_active=active, prefix=prefix, lang=lang)
                )
    for spec in access.CATALOG:
        texts.append(t("bad_args", lang, syntax=spec.syntax))
    sub = SubscriberRef(user_id=1, display_name=EVIL, username="u&v")
    texts += rendering.subscriber_list_text([sub], lang)
    row = SimpleNamespace(chat_id=-1, title=EVIL, registered_by=2)
    texts += rendering.chat_list_text([row], lang, show_registrar=True)
    texts += rendering.startup_report_text([-5], [row], lang)
    texts += rendering.admin_list_text(
        [SimpleNamespace(user_id=1, display_name=EVIL, username=None)], lang
    )
    texts += split_mentions([mention(1, EVIL), mention(2, None)])
    for text in texts:
        assert find_html_errors(text) == [], text
