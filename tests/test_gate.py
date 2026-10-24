from __future__ import annotations

import time

import pytest

from app.gate import ApiGate


def test_rate_limit_min_interval() -> None:
    gate = ApiGate(min_interval_s=0.2, daily_budget=100, jitter_s=0)
    gate.start()
    try:
        times: list[float] = []

        def mark() -> float:
            times.append(time.monotonic())
            return times[-1]

        gate.call(mark)
        gate.call(mark)
        assert len(times) == 2
        assert times[1] - times[0] >= 0.18
    finally:
        gate.stop()


def test_daily_budget_exhausted() -> None:
    gate = ApiGate(min_interval_s=0, daily_budget=2, jitter_s=0)
    gate.start()
    try:
        gate.call(lambda: 1)
        gate.call(lambda: 2)
        with pytest.raises(RuntimeError, match="budget"):
            gate.call(lambda: 3)
    finally:
        gate.stop()


def test_403_sets_skip_window() -> None:
    gate = ApiGate(min_interval_s=0, daily_budget=50, jitter_s=0)
    gate.start()
    try:

        def boom() -> None:
            raise Exception(403, b"forbidden")

        with pytest.raises(Exception):
            gate.call(boom)
        assert gate.skip_current_window is True
        gate.clear_skip()
        assert gate.skip_current_window is False
    finally:
        gate.stop()
