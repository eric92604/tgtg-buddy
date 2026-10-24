from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.predictor import ReleasePredictor, record_observation
from app.scheduler import plan_phases_for_t


def test_prefers_api_window(data_dir: Path) -> None:
    pred = ReleasePredictor()
    api_t = datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc)
    item = {
        "item_id": "abc",
        "next_sales_window_purchase_start": api_t.isoformat(),
    }
    assert pred.predict_from_item(item) == api_t


def test_history_median_same_weekday(data_dir: Path) -> None:
    # Wednesday = weekday 2
    base = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)  # Wed
    for i, minute in enumerate([0, 10, 20]):
        record_observation(
            "item-1",
            stock_appeared_at=base - timedelta(days=7 * i) + timedelta(minutes=minute - 10),
        )
    pred = ReleasePredictor(history_limit=14)
    around = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)  # Wed before window
    got = pred.predict_from_history("item-1", around=around)
    assert got is not None
    assert got.weekday() == 2
    # Median of :50, :00, :10 around 16:00 → ~16:00
    assert got.hour == 16


def test_plan_phases(data_dir: Path) -> None:
    t = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
    now = t - timedelta(seconds=30)  # in warm
    phases = plan_phases_for_t(t, now)
    by_name = {p["name"]: p for p in phases}
    assert by_name["warm"]["active"] is True
    assert by_name["burst"]["active"] is False
    assert by_name["warm"]["interval"] == [8.0, 15.0]
    assert by_name["burst"]["interval"] == [1.5, 3.0]
