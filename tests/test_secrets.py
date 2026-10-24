from __future__ import annotations

from pathlib import Path

import pytest

from app import secrets as secrets_mod


def test_roundtrip_password_and_tokens(data_dir: Path) -> None:
    assert not secrets_mod.has_app_password()
    secrets_mod.set_app_password("hunter2!!")
    assert secrets_mod.has_app_password()
    assert secrets_mod.verify_app_password("hunter2!!")
    assert not secrets_mod.verify_app_password("wrong")

    secrets_mod.set_tgtg_tokens(
        access_token="a",
        refresh_token="r",
        cookie="c=1",
        email="u@example.com",
    )
    tokens = secrets_mod.get_tgtg_tokens()
    assert tokens["access_token"] == "a"
    assert tokens["email"] == "u@example.com"

    secrets_mod.set_card(number="4111111111111111", cvv="123", month="12", year="2030")
    assert secrets_mod.card_configured()
    secrets_mod.set_smtp_password("smtp-secret")
    assert secrets_mod.get_smtp_password() == "smtp-secret"

    enc = data_dir / "secrets.enc"
    assert enc.exists()
    # Ciphertext must not contain plaintext card number.
    assert b"4111111111111111" not in enc.read_bytes()


def test_missing_master_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TGTG_BUDDY_MASTER_KEY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.store as store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path.resolve())
    with pytest.raises(RuntimeError, match="TGTG_BUDDY_MASTER_KEY"):
        secrets_mod.save_secrets({"app_password_hash": None})
