"""
Minimal Vault client using AppRole auth against a KV v2 secrets engine.

Usage:
    from vault_client import load_config_from_vault

    config = load_config_from_vault()
    BOT_TOKEN = config["token"]
    ADMIN_ID = config["admin_id"]
"""

import os
import time
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class VaultAuthError(Exception):
    """Raised when Vault authentication or a Vault request fails."""


class VaultClient:
    """
    Talks to Vault over its HTTP API using AppRole auth.

    Caches the client token in memory and transparently re-authenticates
    when it's about to expire (based on the lease Vault gave us), or if
    Vault rejects a request as unauthorized (e.g. token revoked early).
    """

    def __init__(
        self,
        addr: str,
        role_id: str,
        secret_id: str,
        mount_point: str = "approle",
        kv_mount: str = "secret",
        timeout: float = 5.0,
    ) -> None:
        self.addr = addr.rstrip("/")
        self.role_id = role_id
        self.secret_id = secret_id
        self.mount_point = mount_point
        self.kv_mount = kv_mount
        self.timeout = timeout

        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _login(self) -> None:
        url = f"{self.addr}/v1/auth/{self.mount_point}/login"
        resp = requests.post(
            url,
            json={"role_id": self.role_id, "secret_id": self.secret_id},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise VaultAuthError(
                f"AppRole login failed: {resp.status_code} {resp.text}"
            )

        auth = resp.json()["auth"]
        self._token = auth["client_token"]
        lease_duration = auth.get("lease_duration", 3600)
        # Renew a bit before actual expiry to avoid racing against the deadline.
        self._token_expires_at = time.monotonic() + max(lease_duration - 30, 0)
        logger.info("Vault AppRole login OK, token ttl=%ss", lease_duration)

    def _ensure_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._login()
        assert self._token is not None
        return self._token

    def read_kv(self, path: str, _retry: bool = True) -> dict[str, Any]:
        """
        Read a KV v2 secret at `path` (e.g. "ping-pong-bot").
        Returns the dict of key/value pairs stored there.
        """
        token = self._ensure_token()
        url = f"{self.addr}/v1/{self.kv_mount}/data/{path}"
        resp = requests.get(
            url,
            headers={"X-Vault-Token": token},
            timeout=self.timeout,
        )

        if resp.status_code in (401, 403) and _retry:
            # Token might've been revoked/expired server-side before our
            # local guess — force a fresh login and try exactly once more.
            self._token = None
            return self.read_kv(path, _retry=False)

        if resp.status_code != 200:
            raise VaultAuthError(f"Vault read failed: {resp.status_code} {resp.text}")

        return resp.json()["data"]["data"]


def load_config_from_vault() -> dict[str, Any]:
    """
    Reads bot config from Vault using AppRole creds from environment variables:
      VAULT_ADDR, VAULT_ROLE_ID, VAULT_SECRET_ID, VAULT_SECRET_PATH
    """
    addr = os.environ["VAULT_ADDR"]
    role_id = os.environ["VAULT_ROLE_ID"]
    secret_id = os.environ["VAULT_SECRET_ID"]
    secret_path = os.environ.get("VAULT_SECRET_PATH", "ping-pong-bot")

    client = VaultClient(addr=addr, role_id=role_id, secret_id=secret_id)
    return client.read_kv(secret_path)