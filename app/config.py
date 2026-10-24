"""Non-secret config defaults and helpers for data/config.json."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.store import load_json, save_json

CONFIG_FILE = "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "dry_run": True,
    "pause": False,
    "smtp": {
        "host": "",
        "port": 587,
        "username": "",
        "from_addr": "",
        "to_addr": "",
        "use_tls": True,
    },
    "daily_spend_cap": 0.0,
    "adyen_client_key": "",
}


def default_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG)


def load_config() -> dict[str, Any]:
    """Load config.json merged over defaults (missing keys filled in)."""
    stored = load_json(CONFIG_FILE, default=None)
    cfg = default_config()
    if not isinstance(stored, dict):
        return cfg
    for key, value in stored.items():
        if key == "smtp" and isinstance(value, dict):
            cfg["smtp"].update(value)
        else:
            cfg[key] = value
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    save_json(CONFIG_FILE, cfg)


def ensure_config() -> dict[str, Any]:
    """Create config.json with defaults if it does not exist yet."""
    existing = load_json(CONFIG_FILE, default=None)
    if existing is None:
        cfg = default_config()
        save_config(cfg)
        return cfg
    return load_config()
