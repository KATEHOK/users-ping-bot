import requests
import pytest

from app.config import load_config
from app.vault import VaultClient, VaultError, load_bot_token

SECRET_MARKER = "sup3r-secret-role-id-marker-zzz"
TOKEN_MARKER = "sup3r-secret-token-marker-zzz"


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        item = self.post_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        item = self.get_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _login_ok():
    return FakeResponse(200, {"auth": {"client_token": "vault-client-token"}})


def test_https_enforced():
    with pytest.raises(VaultError):
        VaultClient("http://vault.example.com", "r", "s")


def test_read_kv_success():
    session = FakeSession(
        post_responses=[_login_ok()],
        get_responses=[FakeResponse(200, {"data": {"data": {"token": TOKEN_MARKER}}})],
    )
    client = VaultClient(
        "https://vault.example.com", SECRET_MARKER, "secret-id", session=session
    )
    data = client.read_kv("upb")
    assert data == {"token": TOKEN_MARKER}
    assert len(session.post_calls) == 1
    assert len(session.get_calls) == 1


def test_load_bot_token_missing_token_key():
    session = FakeSession(
        post_responses=[_login_ok()],
        get_responses=[FakeResponse(200, {"data": {"data": {"other": "x"}}})],
    )
    config = load_config(
        {
            "VAULT_ADDR": "https://vault.example.com",
            "VAULT_ROLE_ID": SECRET_MARKER,
            "VAULT_SECRET_ID": "secret-id",
            "VAULT_SECRET_PATH": "upb",
        }
    )
    with pytest.raises(VaultError) as exc_info:
        load_bot_token(config, session=session)
    assert SECRET_MARKER not in str(exc_info.value)


def test_load_bot_token_success():
    session = FakeSession(
        post_responses=[_login_ok()],
        get_responses=[FakeResponse(200, {"data": {"data": {"token": TOKEN_MARKER}}})],
    )
    config = load_config(
        {
            "VAULT_ADDR": "https://vault.example.com",
            "VAULT_ROLE_ID": SECRET_MARKER,
            "VAULT_SECRET_ID": "secret-id",
            "VAULT_SECRET_PATH": "upb",
        }
    )
    assert load_bot_token(config, session=session) == TOKEN_MARKER


def test_read_kv_401_forces_single_relogin_then_succeeds():
    session = FakeSession(
        post_responses=[_login_ok(), _login_ok()],
        get_responses=[
            FakeResponse(401, text="unauthorized body should never leak"),
            FakeResponse(200, {"data": {"data": {"token": TOKEN_MARKER}}}),
        ],
    )
    client = VaultClient(
        "https://vault.example.com", SECRET_MARKER, "secret-id", session=session
    )
    data = client.read_kv("upb")
    assert data == {"token": TOKEN_MARKER}
    assert len(session.post_calls) == 2  # forced re-login happened exactly once
    assert len(session.get_calls) == 2


def test_read_kv_401_twice_fails_without_second_relogin():
    session = FakeSession(
        post_responses=[_login_ok(), _login_ok()],
        get_responses=[
            FakeResponse(401, text=SECRET_MARKER),
            FakeResponse(401, text=SECRET_MARKER),
        ],
    )
    client = VaultClient(
        "https://vault.example.com", SECRET_MARKER, "secret-id", session=session
    )
    with pytest.raises(VaultError) as exc_info:
        client.read_kv("upb")
    assert SECRET_MARKER not in str(exc_info.value)
    assert len(session.post_calls) == 2  # exactly one forced retry, not more


def test_read_kv_5xx_retries_with_bounded_attempts_then_fails():
    session = FakeSession(
        post_responses=[_login_ok()],
        get_responses=[
            FakeResponse(503, text=SECRET_MARKER),
            FakeResponse(503, text=SECRET_MARKER),
            FakeResponse(503, text=SECRET_MARKER),
        ],
    )
    client = VaultClient(
        "https://vault.example.com",
        SECRET_MARKER,
        "secret-id",
        session=session,
        max_attempts=3,
    )
    with pytest.raises(VaultError) as exc_info:
        client.read_kv("upb")
    assert "503" in str(exc_info.value)
    assert SECRET_MARKER not in str(exc_info.value)
    assert len(session.get_calls) == 3


def test_read_kv_timeout_retries_then_fails():
    session = FakeSession(
        post_responses=[_login_ok()],
        get_responses=[
            requests.exceptions.Timeout(),
            requests.exceptions.Timeout(),
        ],
    )
    client = VaultClient(
        "https://vault.example.com",
        SECRET_MARKER,
        "secret-id",
        session=session,
        max_attempts=2,
    )
    with pytest.raises(VaultError) as exc_info:
        client.read_kv("upb")
    assert SECRET_MARKER not in str(exc_info.value)
    assert "token" not in str(exc_info.value).lower() or TOKEN_MARKER not in str(exc_info.value)


def test_login_tls_failure_gives_safe_diagnosis_no_secrets():
    # an invalid/untrusted TLS certificate surfaces as requests.exceptions.SSLError,
    # a RequestException subclass; it must map to a safe VaultError, never a raw
    # exception message that could carry a hostname, path or certificate detail.
    session = FakeSession(
        post_responses=[
            requests.exceptions.SSLError(f"certificate verify failed for {SECRET_MARKER}.example")
        ],
        get_responses=[],
    )
    client = VaultClient(
        "https://vault.example.com", SECRET_MARKER, "the-secret-id", session=session
    )
    with pytest.raises(VaultError) as exc_info:
        client.read_kv("upb")
    message = str(exc_info.value)
    assert SECRET_MARKER not in message
    assert "certificate" not in message
    assert "SSLError" in message  # safe: only the exception class name is reported


def test_login_failure_never_leaks_body_or_ids():
    session = FakeSession(
        post_responses=[FakeResponse(400, text=f"bad request {SECRET_MARKER}")],
        get_responses=[],
    )
    client = VaultClient(
        "https://vault.example.com", SECRET_MARKER, "the-secret-id", session=session
    )
    with pytest.raises(VaultError) as exc_info:
        client.read_kv("upb")
    message = str(exc_info.value)
    assert SECRET_MARKER not in message
    assert "the-secret-id" not in message
    assert "bad request" not in message
    assert "400" in message
