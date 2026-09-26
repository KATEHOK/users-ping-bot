import time
from typing import Any

import requests

from .clock import SYSTEM_CLOCK, Clock
from .config import Config


class VaultError(Exception):
    pass


class VaultClient:
    def __init__(
        self,
        addr: str,
        role_id: str,
        secret_id: str,
        *,
        auth_mount: str = "approle",
        kv_mount: str = "secret",
        ca_path: str | None = None,
        timeout: float = 5.0,
        max_attempts: int = 3,
        session: Any = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if not addr.startswith("https://"):
            raise VaultError("vault addr must use https")
        self._addr = addr.rstrip("/")
        self._role_id = role_id
        self._secret_id = secret_id
        self._auth_mount = auth_mount
        self._kv_mount = kv_mount
        self._verify = ca_path or True
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._session = session or requests.Session()
        self._clock = clock
        self._token: str | None = None

    # backoff sleeps use wall-clock time directly: read_kv/_login are sync,
    # Clock.sleep is async, so it can't be awaited here. clock is kept for
    # signature parity with other components and possible future use.
    def _backoff(self, attempt: int) -> None:
        time.sleep(min(0.05 * (2 ** (attempt - 1)), 1.0))

    def _login(self) -> None:
        attempt = 1
        while True:
            try:
                resp = self._session.post(
                    f"{self._addr}/v1/auth/{self._auth_mount}/login",
                    json={"role_id": self._role_id, "secret_id": self._secret_id},
                    timeout=self._timeout,
                    verify=self._verify,
                )
            except requests.exceptions.Timeout:
                if attempt >= self._max_attempts:
                    raise VaultError("vault login timeout") from None
                self._backoff(attempt)
                attempt += 1
                continue
            except requests.exceptions.RequestException as exc:
                raise VaultError(f"vault login request failed: {type(exc).__name__}") from None

            if resp.status_code >= 500:
                if attempt >= self._max_attempts:
                    raise VaultError(f"vault login failed: status={resp.status_code}")
                self._backoff(attempt)
                attempt += 1
                continue

            if resp.status_code != 200:
                raise VaultError(f"vault login failed: status={resp.status_code}")

            break

        try:
            token = resp.json()["auth"]["client_token"]
        except (KeyError, TypeError, ValueError):
            raise VaultError("vault login returned malformed response") from None
        if not isinstance(token, str) or not token:
            raise VaultError("vault login returned malformed response")
        self._token = token

    def _ensure_token(self) -> str:
        if self._token is None:
            self._login()
        assert self._token is not None
        return self._token

    def read_kv(self, path: str) -> dict[str, Any]:
        attempt = 1
        reauthed = False
        resp = None
        while True:
            token = self._ensure_token()
            try:
                resp = self._session.get(
                    f"{self._addr}/v1/{self._kv_mount}/data/{path}",
                    headers={"X-Vault-Token": token},
                    timeout=self._timeout,
                    verify=self._verify,
                )
            except requests.exceptions.Timeout:
                if attempt >= self._max_attempts:
                    raise VaultError("vault read_kv timeout") from None
                self._backoff(attempt)
                attempt += 1
                continue
            except requests.exceptions.RequestException as exc:
                raise VaultError(f"vault read_kv request failed: {type(exc).__name__}") from None

            if resp.status_code in (401, 403) and not reauthed:
                reauthed = True
                self._token = None
                continue

            if resp.status_code >= 500:
                if attempt >= self._max_attempts:
                    raise VaultError(f"vault read_kv failed: status={resp.status_code}")
                self._backoff(attempt)
                attempt += 1
                continue

            if resp.status_code != 200:
                raise VaultError(f"vault read_kv failed: status={resp.status_code}")

            break

        try:
            data = resp.json()["data"]["data"]
        except (KeyError, TypeError, ValueError):
            raise VaultError("vault read_kv returned malformed response") from None
        if not isinstance(data, dict):
            raise VaultError("vault read_kv returned malformed response")
        return data


def load_bot_token(config: Config, *, session: Any = None) -> str:
    client = VaultClient(
        config.vault_addr,
        config.vault_role_id,
        config.vault_secret_id,
        auth_mount=config.vault_auth_mount,
        kv_mount=config.vault_kv_mount,
        ca_path=config.vault_ca_path,
        timeout=config.vault_timeout,
        session=session,
    )
    data = client.read_kv(config.vault_secret_path)
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise VaultError("vault secret is missing 'token'")
    return token
