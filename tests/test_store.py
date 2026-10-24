from __future__ import annotations

from pathlib import Path

from app.store import load_json, save_json


def test_atomic_roundtrip(data_dir: Path) -> None:
    save_json("watched.json", [{"item_id": "1"}])
    assert load_json("watched.json") == [{"item_id": "1"}]
    assert (data_dir / "watched.json").exists()
    assert load_json("missing.json", default=[]) == []
