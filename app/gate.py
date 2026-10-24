"""ApiGate — single worker thread, rate limit, daily budget, backoff."""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class GateStats:
    calls_today: int = 0
    day: date = field(default_factory=date.today)
    last_call_at: float = 0.0
    backoff_until: float = 0.0
    consecutive_errors: int = 0


class ApiGate:
    """Serialize tgtg API calls through one worker with rate limiting."""

    def __init__(
        self,
        *,
        min_interval_s: float = 1.0,
        daily_budget: int = 2000,
        jitter_s: float = 0.15,
    ) -> None:
        self.min_interval_s = min_interval_s
        self.daily_budget = daily_budget
        self.jitter_s = jitter_s
        self._q: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._stats = GateStats()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, name="ApiGate", daemon=True)
        self._started = False
        self._skip_window = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def stop(self) -> None:
        if self._started:
            self._q.put(None)
            self._thread.join(timeout=5)
            self._started = False

    @property
    def skip_current_window(self) -> bool:
        return self._skip_window

    def clear_skip(self) -> None:
        self._skip_window = False

    def calls_today(self) -> int:
        with self._lock:
            self._rollover_day()
            return self._stats.calls_today

    def _rollover_day(self) -> None:
        today = date.today()
        if self._stats.day != today:
            self._stats.day = today
            self._stats.calls_today = 0

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        """Enqueue a callable; returns a Future for its result."""
        if not self._started:
            self.start()
        fut: Future[T] = Future()
        self._q.put((fn, fut))
        return fut

    def call(self, fn: Callable[[], T], timeout: float | None = 120.0) -> T:
        """Submit and wait for the result."""
        return self.submit(fn).result(timeout=timeout)

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, fut = item
            try:
                self._throttle()
                result = fn()
                with self._lock:
                    self._stats.consecutive_errors = 0
                fut.set_result(result)
            except Exception as exc:
                self._handle_error(exc)
                fut.set_exception(exc)

    def _throttle(self) -> None:
        with self._lock:
            self._rollover_day()
            if self._stats.calls_today >= self.daily_budget:
                raise RuntimeError(
                    f"Daily API budget exhausted ({self.daily_budget} calls)"
                )
            now = time.monotonic()
            wait_backoff = max(0.0, self._stats.backoff_until - now)
            since_last = now - self._stats.last_call_at
            wait_rate = max(0.0, self.min_interval_s - since_last)
            wait = max(wait_backoff, wait_rate)
            if self.jitter_s > 0:
                wait += random.uniform(0, self.jitter_s)
            self._stats.calls_today += 1
            self._stats.last_call_at = now + wait
        if wait > 0:
            time.sleep(wait)

    def _handle_error(self, exc: Exception) -> None:
        status = getattr(exc, "args", [None])[0] if exc.args else None
        # TgtgAPIError(status_code, content)
        code = status if isinstance(status, int) else None
        with self._lock:
            self._stats.consecutive_errors += 1
            n = self._stats.consecutive_errors
            if code in (403, 429) or "429" in str(exc) or "403" in str(exc):
                backoff = min(300.0, (2 ** min(n, 8)) + random.uniform(0, 1))
                self._stats.backoff_until = time.monotonic() + backoff
                self._skip_window = True
                logger.warning(
                    "API gate backoff %.1fs after %s (skip window)", backoff, exc
                )
            else:
                logger.warning("API gate call failed: %s", exc)
