"""Shared fixtures — isolated DATA_DIR + master key."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TGTG_BUDDY_MASTER_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.store as store

    resolved = tmp_path.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", resolved)
    return resolved
