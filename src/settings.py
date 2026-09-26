from vault_client import load_config_from_vault

def _normalize_ids(raw) -> set[int]:
    if isinstance(raw, str):
        import json
        raw = json.loads(raw)
    return {int(x) for x in raw}

config = load_config_from_vault()

class Settings:
    BOT_TOKEN = config["token"]
    ADMIN_ID = int(config["admin_id"])