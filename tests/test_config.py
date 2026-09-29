import pytest

from app.config import Config, ConfigError, load_config, load_db_path


def _env(**overrides):
    base = {
        "VAULT_ADDR": "https://vault.example.com",
        "VAULT_ROLE_ID": "role-secret-marker",
        "VAULT_SECRET_ID": "secret-secret-marker",
        "VAULT_SECRET_PATH": "upb",
    }
    base.update(overrides)
    return base


def test_load_config_defaults():
    cfg = load_config(_env())
    assert cfg.vault_addr == "https://vault.example.com"
    assert cfg.vault_auth_mount == "approle"
    assert cfg.vault_kv_mount == "secret"
    assert cfg.vault_ca_path is None
    assert cfg.vault_timeout == 5.0
    assert cfg.db_path == "/data/upb.sqlite3"
    assert cfg.log_level == "INFO"
    assert cfg.ping_cooldown_seconds == 5.0


def test_load_config_overrides():
    cfg = load_config(
        _env(
            VAULT_AUTH_MOUNT="approle2",
            VAULT_KV_MOUNT="kv2",
            VAULT_CA_PATH="/etc/ca.pem",
            VAULT_TIMEOUT="2.5",
            UPB_DB_PATH="/data/other.sqlite3",
            UPB_LOG_LEVEL="DEBUG",
            UPB_PING_COOLDOWN_SECONDS="0.5",
        )
    )
    assert cfg.vault_auth_mount == "approle2"
    assert cfg.vault_kv_mount == "kv2"
    assert cfg.vault_ca_path == "/etc/ca.pem"
    assert cfg.vault_timeout == 2.5
    assert cfg.db_path == "/data/other.sqlite3"
    assert cfg.log_level == "DEBUG"
    assert cfg.ping_cooldown_seconds == 0.5


@pytest.mark.parametrize(
    "missing", ["VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_SECRET_PATH"]
)
def test_missing_required_raises_with_var_name(missing):
    env = _env()
    del env[missing]
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    assert str(exc_info.value) == missing


def test_bad_timeout_raises_config_error():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(VAULT_TIMEOUT="not-a-number"))
    assert str(exc_info.value) == "VAULT_TIMEOUT"


def test_repr_masks_secrets():
    cfg = load_config(_env())
    text = repr(cfg)
    assert "role-secret-marker" not in text
    assert "secret-secret-marker" not in text
    assert "vault.example.com" in text


def test_config_is_frozen():
    cfg = load_config(_env())
    with pytest.raises(Exception):
        cfg.vault_addr = "https://other"


def test_load_db_path_no_vault_vars_needed():
    assert load_db_path({}) == "/data/upb.sqlite3"
    assert load_db_path({"UPB_DB_PATH": "/data/custom.sqlite3"}) == "/data/custom.sqlite3"


def test_config_import_has_no_side_effects(monkeypatch):
    # calling load_config with an explicit mapping must never touch os.environ
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    with pytest.raises(ConfigError):
        load_config({})


def test_ping_cooldown_empty_uses_default_and_bad_value_raises():
    assert load_config(_env(UPB_PING_COOLDOWN_SECONDS="")).ping_cooldown_seconds == 5.0
    with pytest.raises(ConfigError) as exc:
        load_config(_env(UPB_PING_COOLDOWN_SECONDS="soon"))
    assert "UPB_PING_COOLDOWN_SECONDS" in str(exc.value)
