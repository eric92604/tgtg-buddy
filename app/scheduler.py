"""WindowScheduler — discovery + warm/burst/tail polling windows."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app import config as config_mod
from app import secrets as secrets_mod
from app.gate import ApiGate
from app.predictor import ReleasePredictor, record_observation
from app.purchaser import Purchaser
from app.store import load_json, save_json
from app.tgtg_client import BuddyClient

logger = logging.getLogger(__name__)

WATCHED_FILE = "watched.json"


def load_watched() -> list[dict[str, Any]]:
    data = load_json(WATCHED_FILE, default=[])
    return data if isinstance(data, list) else []


def save_watched(rows: list[dict[str, Any]]) -> None:
    save_json(WATCHED_FILE, rows)


def upsert_watched(item: dict[str, Any]) -> None:
    rows = load_watched()
    item_id = str(item["item_id"])
    for i, row in enumerate(rows):
        if str(row.get("item_id")) == item_id:
            rows[i] = {**row, **item}
            save_watched(rows)
            return
    rows.append(item)
    save_watched(rows)


def remove_watched(item_id: str) -> None:
    save_watched([r for r in load_watched() if str(r.get("item_id")) != str(item_id)])


@dataclass
class WindowPhase:
    name: str
    start_offset_s: float  # relative to T
    end_offset_s: float
    interval_min_s: float
    interval_max_s: float


# Per plan: warm T−90…T−10 (8–15s), burst T−10…T+45 (1.5–3s), tail T+45…T+3m (10–20s)
WINDOW_PHASES = [
    WindowPhase("warm", -90, -10, 8.0, 15.0),
    WindowPhase("burst", -10, 45, 1.5, 3.0),
    WindowPhase("tail", 45, 180, 10.0, 20.0),
]


@dataclass
class PlannedWindow:
    item_ids: set[str] = field(default_factory=set)
    t: datetime | None = None


class WindowScheduler:
    def __init__(
        self,
        gate: ApiGate,
        *,
        notify_send: Callable[[str, str], Any] | None = None,
        client_factory: Callable[[], BuddyClient] | None = None,
    ) -> None:
        self.gate = gate
        self.predictor = ReleasePredictor()
        self.notify_send = notify_send
        self._client_factory = client_factory or self._default_client
        self.purchaser = Purchaser(
            gate_call=self.gate.call,
            client_factory=self._client_factory,
            notify_send=notify_send,
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._next_discovery: datetime | None = None
        self._stock_seen: dict[str, bool] = {}

    def _default_client(self) -> BuddyClient:
        tokens = secrets_mod.get_tgtg_tokens()
        cfg = config_mod.load_config()
        return BuddyClient(
            access_token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            cookie=tokens.get("cookie"),
            email=tokens.get("email"),
            adyen_client_key=cfg.get("adyen_client_key") or None,
        )

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self.gate.start()
            self._task = asyncio.create_task(self._run(), name="WindowScheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        self.gate.stop()

    async def _run(self) -> None:
        logger.info("WindowScheduler started")
        self._schedule_next_discovery(initial=True)
        while not self._stop.is_set():
            try:
                cfg = config_mod.load_config()
                if cfg.get("pause"):
                    await self._sleep(5)
                    continue
                if not secrets_mod.get_tgtg_tokens().get("access_token"):
                    await self._sleep(10)
                    continue

                now = datetime.now(timezone.utc)
                if self._next_discovery is None or now >= self._next_discovery:
                    await self._discovery()
                    self._schedule_next_discovery(initial=False)

                watched = load_watched()
                windows = self._active_windows(watched, now)
                if not windows:
                    await self._sleep(2)
                    continue

                if self.gate.skip_current_window:
                    logger.info("Skipping window due to gate backoff")
                    await self._sleep(5)
                    # Clear after one wait so a new window can try.
                    self.gate.clear_skip()
                    continue

                await self._poll_batch(watched, windows)
                interval = self._next_interval(windows, now)
                await self._sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler loop error")
                await self._sleep(5)
        logger.info("WindowScheduler stopped")

    def _schedule_next_discovery(self, *, initial: bool) -> None:
        # Jittered daily discovery: soon on start, then ~24h + jitter.
        now = datetime.now(timezone.utc)
        if initial:
            self._next_discovery = now + timedelta(seconds=random.uniform(2, 10))
        else:
            self._next_discovery = now + timedelta(
                hours=24, seconds=random.uniform(0, 3600)
            )

    async def _discovery(self) -> None:
        logger.info("Running daily discovery")
        watched = load_watched()
        if not watched:
            return
        client = self._client_factory()
        try:
            favorites = await asyncio.to_thread(
                self.gate.call, lambda: client.get_favorites()
            )
        except Exception:
            logger.exception("get_favorites failed")
            favorites = []

        fav_by_id = {}
        for fav in favorites or []:
            iid = str((fav.get("item") or {}).get("item_id") or fav.get("item_id") or "")
            if iid:
                fav_by_id[iid] = fav

        for row in watched:
            item_id = str(row.get("item_id"))
            await self._sleep(random.uniform(20, 60))
            if self._stop.is_set():
                return
            try:
                detail = await asyncio.to_thread(
                    self.gate.call, lambda iid=item_id: client.get_item(iid)
                )
            except Exception:
                logger.exception("get_item(%s) failed", item_id)
                detail = fav_by_id.get(item_id)

            if not detail:
                continue
            t = self.predictor.predict_from_item(detail)
            name = (
                (detail.get("item") or {}).get("name")
                or detail.get("display_name")
                or row.get("display_name")
                or item_id
            )
            store = (detail.get("store") or {}).get("store_name") or ""
            display = f"{store} — {name}" if store else name
            row["display_name"] = display
            row["next_window"] = t.isoformat() if t else None
            row["items_available"] = int(
                detail.get("items_available")
                or (detail.get("item") or {}).get("items_available")
                or 0
            )
            upsert_watched(row)

        # Persist credentials if refreshed.
        creds = client.persistable_credentials()
        if creds.get("access_token"):
            secrets_mod.set_tgtg_tokens(**creds)

    def _active_windows(
        self, watched: list[dict[str, Any]], now: datetime
    ) -> list[tuple[dict[str, Any], datetime, WindowPhase]]:
        active: list[tuple[dict[str, Any], datetime, WindowPhase]] = []
        for row in watched:
            t = None
            if row.get("next_window"):
                try:
                    text = str(row["next_window"])
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    t = datetime.fromisoformat(text)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                except ValueError:
                    t = None
            if t is None:
                continue
            offset = (now - t).total_seconds()
            for phase in WINDOW_PHASES:
                if phase.start_offset_s <= offset < phase.end_offset_s:
                    active.append((row, t, phase))
                    break
        return active

    def _next_interval(
        self,
        windows: list[tuple[dict[str, Any], datetime, WindowPhase]],
        now: datetime,
    ) -> float:
        if not windows:
            return 2.0
        # Use the tightest (burstiest) phase among active windows.
        phase = min(windows, key=lambda w: w[2].interval_min_s)[2]
        return random.uniform(phase.interval_min_s, phase.interval_max_s)

    async def _poll_batch(
        self,
        watched: list[dict[str, Any]],
        windows: list[tuple[dict[str, Any], datetime, WindowPhase]],
    ) -> None:
        watched_ids = {str(w.get("item_id")) for w, _, _ in windows}
        client = self._client_factory()
        try:
            items = await asyncio.to_thread(
                self.gate.call,
                lambda: client.get_items(favorites_only=True, with_stock_only=False),
            )
        except Exception:
            logger.exception("batch get_items failed")
            return

        by_id: dict[str, dict[str, Any]] = {}
        for it in items or []:
            iid = str((it.get("item") or {}).get("item_id") or it.get("item_id") or "")
            if iid:
                by_id[iid] = it

        watched_map = {str(r.get("item_id")): r for r in watched}
        for item_id in watched_ids:
            item = by_id.get(item_id)
            if not item:
                continue
            available = int(
                item.get("items_available")
                or (item.get("item") or {}).get("items_available")
                or 0
            )
            if available > 0 and not self._stock_seen.get(item_id):
                self._stock_seen[item_id] = True
                record_observation(item_id, stock_appeared_at=datetime.now(timezone.utc))
            elif available <= 0:
                self._stock_seen[item_id] = False

            if available > 0:
                row = watched_map.get(item_id) or {"item_id": item_id, "qty": 1}
                try:
                    await asyncio.to_thread(self.purchaser.try_purchase, item, row)
                except Exception:
                    logger.exception("purchase attempt failed for %s", item_id)

        creds = client.persistable_credentials()
        if creds.get("access_token"):
            secrets_mod.set_tgtg_tokens(**creds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def plan_phases_for_t(t: datetime, now: datetime | None = None) -> list[dict[str, Any]]:
    """Helper used by tests/UI: describe phase schedule around T."""
    now = now or datetime.now(timezone.utc)
    out = []
    for phase in WINDOW_PHASES:
        start = t + timedelta(seconds=phase.start_offset_s)
        end = t + timedelta(seconds=phase.end_offset_s)
        out.append(
            {
                "name": phase.name,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "active": start <= now < end,
                "interval": [phase.interval_min_s, phase.interval_max_s],
            }
        )
    return out
