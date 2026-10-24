"""Fernet-encrypted secrets blob at data/secrets.enc."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from app.store import data_path, ensure_data_dir

SECRETS_FILE = "secrets.enc"
MASTER_KEY_ENV = "TGTG_BUDDY_MASTER_KEY"


def _master_key() -> bytes:
    raw = os.environ.get(MASTER_KEY_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            f"{MASTER_KEY_ENV} is not set. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    # Accept a raw Fernet key, or derive one from an arbitrary passphrase.
    try:
        Fernet(raw.encode() if isinstance(raw, str) else raw)
        return raw.encode() if isinstance(raw, str) else raw
    except Exception:
        digest = hashlib.sha256(raw.encode()).digest()
        return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_master_key())


def secrets_path() -> Path:
    # Resolve via data_path so DATA_DIR changes (tests) are picked up.
    return data_path(SECRETS_FILE)


def secrets_exist() -> bool:
    return secrets_path().exists()


def _empty_secrets() -> dict[str, Any]:
    return {
        "app_password_hash": None,
        "tgtg": {
            "access_token": None,
            "refresh_token": None,
            "cookie": None,
            "email": None,
        },
        "card": {
            "number": None,
            "cvv": None,
            "month": None,
            "year": None,
            "holder": None,
        },
        "smtp_password": None,
    }


def load_secrets() -> dict[str, Any]:
    path = secrets_path()
    if not path.exists():
        return _empty_secrets()
    try:
        token = path.read_bytes()
        plain = _fernet().decrypt(token)
        data = json.loads(plain.decode("utf-8"))
        base = _empty_secrets()
        if not isinstance(data, dict):
            return base
        for key, value in data.items():
            if key in ("tgtg", "card") and isinstance(value, dict):
                base[key].update(value)
            else:
                base[key] = value
        return base
    except (InvalidToken, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Failed to decrypt {SECRETS_FILE}: {exc}") from exc


def save_secrets(data: dict[str, Any]) -> None:
    ensure_data_dir()
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    token = _fernet().encrypt(payload)
    path = secrets_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(token)
    os.replace(tmp, path)


def has_app_password() -> bool:
    secrets = load_secrets()
    return bool(secrets.get("app_password_hash"))


def set_app_password(password: str) -> None:
    secrets = load_secrets()
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    secrets["app_password_hash"] = hashed.decode("utf-8")
    save_secrets(secrets)


def verify_app_password(password: str) -> bool:
    secrets = load_secrets()
    stored = secrets.get("app_password_hash")
    if not stored:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def get_tgtg_tokens() -> dict[str, str | None]:
    return dict(load_secrets().get("tgtg") or {})


def set_tgtg_tokens(
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    cookie: str | None = None,
    email: str | None = None,
) -> None:
    secrets = load_secrets()
    tgtg = secrets.setdefault("tgtg", {})
    if access_token is not None:
        tgtg["access_token"] = access_token
    if refresh_token is not None:
        tgtg["refresh_token"] = refresh_token
    if cookie is not None:
        tgtg["cookie"] = cookie
    if email is not None:
        tgtg["email"] = email
    save_secrets(secrets)


def clear_tgtg_tokens() -> None:
    secrets = load_secrets()
    secrets["tgtg"] = {
        "access_token": None,
        "refresh_token": None,
        "cookie": None,
        "email": secrets.get("tgtg", {}).get("email"),
    }
    save_secrets(secrets)


def get_card() -> dict[str, str | None]:
    return dict(load_secrets().get("card") or {})


def set_card(
    *,
    number: str | None = None,
    cvv: str | None = None,
    month: str | None = None,
    year: str | None = None,
    holder: str | None = None,
) -> None:
    secrets = load_secrets()
    card = secrets.setdefault("card", {})
    if number is not None:
        card["number"] = number
    if cvv is not None:
        card["cvv"] = cvv
    if month is not None:
        card["month"] = month
    if year is not None:
        card["year"] = year
    if holder is not None:
        card["holder"] = holder
    save_secrets(secrets)


def card_configured() -> bool:
    card = get_card()
    return bool(card.get("number") and card.get("cvv") and card.get("month") and card.get("year"))


def get_smtp_password() -> str | None:
    return load_secrets().get("smtp_password")


def set_smtp_password(password: str | None) -> None:
    secrets = load_secrets()
    secrets["smtp_password"] = password
    save_secrets(secrets)


def session_secret_key() -> str:
    """Stable session signing key derived from the master key."""
    return hashlib.sha256(_master_key()).hexdigest()
