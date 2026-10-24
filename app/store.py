"""Atomic JSON load/save helpers for the data/ directory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()


def data_path(name: str) -> Path:
    """Return an absolute path under DATA_DIR for a relative filename."""
    return DATA_DIR / name


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def load_json(name: str, default: Any = None) -> Any:
    """Load a JSON file from data/. Returns *default* if missing or empty."""
    path = data_path(name)
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return default
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(name: str, data: Any) -> None:
    """Atomically write JSON to data/ via tempfile + os.replace."""
    ensure_data_dir()
    path = data_path(name)
    fd, tmp_name = tempfile.mkstemp(
        dir=DATA_DIR,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
