"""ReleasePredictor — API window time with observation-based fallback."""

from __future__ import annotations

import statistics
from datetime import datetime, time, timedelta, timezone
from typing import Any

from app.store import load_json, save_json

OBSERVATIONS_FILE = "observations.json"
MAX_OBSERVATIONS = 200


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_observations() -> list[dict[str, Any]]:
    data = load_json(OBSERVATIONS_FILE, default=[])
    return data if isinstance(data, list) else []


def save_observations(rows: list[dict[str, Any]]) -> None:
    save_json(OBSERVATIONS_FILE, rows[-MAX_OBSERVATIONS:])


def record_observation(
    item_id: str,
    *,
    stock_appeared_at: datetime | str | None = None,
    sold_out_at: datetime | str | None = None,
    source: str = "poll",
) -> None:
    appeared = _parse_dt(stock_appeared_at) or datetime.now(timezone.utc)
    rows = load_observations()
    rows.append(
        {
            "item_id": str(item_id),
            "stock_appeared_at": appeared.isoformat(),
            "sold_out_at": (_parse_dt(sold_out_at).isoformat() if sold_out_at else None),
            "weekday": appeared.astimezone(timezone.utc).weekday(),
            "source": source,
        }
    )
    save_observations(rows)


class ReleasePredictor:
    """Predict next release time T for an item."""

    def __init__(self, history_limit: int = 14) -> None:
        self.history_limit = history_limit

    def predict_from_item(self, item: dict[str, Any]) -> datetime | None:
        """Prefer API next_sales_window_purchase_start; else history median."""
        api_time = self._api_window(item)
        if api_time is not None:
            return api_time
        item_id = self._item_id(item)
        if not item_id:
            return None
        return self.predict_from_history(item_id)

    def predict_from_history(
        self, item_id: str, *, around: datetime | None = None
    ) -> datetime | None:
        """Median of last N same-weekday observations, projected to next occurrence."""
        around = around or datetime.now(timezone.utc)
        rows = [
            r
            for r in load_observations()
            if str(r.get("item_id")) == str(item_id) and r.get("stock_appeared_at")
        ]
        if not rows:
            return None

        target_weekday = around.astimezone(timezone.utc).weekday()
        same_day = [
            r
            for r in rows
            if int(r.get("weekday", -1)) == target_weekday
            or _parse_dt(r["stock_appeared_at"]).weekday() == target_weekday  # type: ignore[union-attr]
        ]
        pool = same_day or rows
        pool = pool[-self.history_limit :]
        times_of_day: list[time] = []
        for row in pool:
            dt = _parse_dt(row["stock_appeared_at"])
            if dt is None:
                continue
            dt_utc = dt.astimezone(timezone.utc)
            times_of_day.append(dt_utc.timetz().replace(tzinfo=None))

        if not times_of_day:
            return None

        seconds = [t.hour * 3600 + t.minute * 60 + t.second for t in times_of_day]
        median_s = int(statistics.median(seconds))
        hh, rem = divmod(median_s, 3600)
        mm, ss = divmod(rem, 60)
        candidate = around.astimezone(timezone.utc).replace(
            hour=hh, minute=mm, second=ss, microsecond=0
        )
        # If we're past today's median for this weekday, jump to next matching weekday.
        if candidate <= around.astimezone(timezone.utc):
            candidate += timedelta(days=1)
        while candidate.weekday() != target_weekday and same_day:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _api_window(item: dict[str, Any]) -> datetime | None:
        # Field may live at top level or under purchase_window / sales_window.
        candidates = [
            item.get("next_sales_window_purchase_start"),
            (item.get("purchase_window") or {}).get("start_date"),
            (item.get("sales_window") or {}).get("purchase_start"),
        ]
        for value in candidates:
            dt = _parse_dt(value)
            if dt is not None:
                return dt
        return None

    @staticmethod
    def _item_id(item: dict[str, Any]) -> str | None:
        if "item_id" in item:
            return str(item["item_id"])
        nested = item.get("item") or {}
        if isinstance(nested, dict) and nested.get("item_id"):
            return str(nested["item_id"])
        if item.get("id"):
            return str(item["id"])
        return None
