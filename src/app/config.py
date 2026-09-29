import os
from dataclasses import dataclass
from typing import Mapping


class ConfigError(Exception):
    pass


def _mask(_value: str) -> str:
    return "***"


@dataclass(frozen=True, slots=True)
class Config:
    vault_addr: str
    vault_role_id: str
    vault_secret_id: str
    vault_secret_path: str
    vault_auth_mount: str = "approle"
    vault_kv_mount: str = "secret"
    vault_ca_path: str | None = None
    vault_timeout: float = 5.0
    db_path: str = "/data/upb.sqlite3"
    log_level: str = "INFO"
    ping_cooldown_seconds: float = 5.0

    def __repr__(self) -> str:
        return (
            "Config(vault_addr=%r, vault_role_id=%s, vault_secret_id=%s, "
            "vault_secret_path=%r, vault_auth_mount=%r, vault_kv_mount=%r, "
            "vault_ca_path=%r, vault_timeout=%r, db_path=%r, log_level=%r, ping_cooldown_seconds=%r)"
            % (
                self.vault_addr,
                _mask(self.vault_role_id),
                _mask(self.vault_secret_id),
                self.vault_secret_path,
                self.vault_auth_mount,
                self.vault_kv_mount,
                self.vault_ca_path,
                self.vault_timeout,
                self.db_path,
                self.log_level,
                self.ping_cooldown_seconds,
            )
        )


_REQUIRED = ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_SECRET_PATH")
_DEFAULT_DB_PATH = "/data/upb.sqlite3"


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ConfigError(name)
    return value


def _optional_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(name) from None


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = env if env is not None else os.environ
    values = {name: _require(env, name) for name in _REQUIRED}
    return Config(
        vault_addr=values["VAULT_ADDR"],
        vault_role_id=values["VAULT_ROLE_ID"],
        vault_secret_id=values["VAULT_SECRET_ID"],
        vault_secret_path=values["VAULT_SECRET_PATH"],
        vault_auth_mount=env.get("VAULT_AUTH_MOUNT") or "approle",
        vault_kv_mount=env.get("VAULT_KV_MOUNT") or "secret",
        vault_ca_path=env.get("VAULT_CA_PATH") or None,
        vault_timeout=_optional_float(env, "VAULT_TIMEOUT", 5.0),
        db_path=env.get("UPB_DB_PATH") or _DEFAULT_DB_PATH,
        log_level=env.get("UPB_LOG_LEVEL") or "INFO",
        ping_cooldown_seconds=_optional_float(env, "UPB_PING_COOLDOWN_SECONDS", 5.0),
    )


def load_db_path(env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    return env.get("UPB_DB_PATH") or _DEFAULT_DB_PATH
