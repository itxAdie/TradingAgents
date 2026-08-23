"""Phase 2 — SimulationClock determinism and safety."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.backtest.clock import SimulationClock


def _ts(**kwargs) -> datetime:
    return datetime(2025, 6, 1, 12, tzinfo=timezone.utc) + timedelta(**kwargs)


def test_clock_requires_tz_aware_start() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        SimulationClock(datetime(2025, 6, 1, 12))  # naive


def test_clock_set_rejects_naive_datetime() -> None:
    clock = SimulationClock(_ts())
    with pytest.raises(ValueError, match="tz-aware"):
        clock.set(datetime(2025, 6, 2, 12))  # naive


def test_clock_normalizes_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    clock = SimulationClock(datetime(2025, 6, 1, 14, tzinfo=plus_two))
    assert clock.now() == datetime(2025, 6, 1, 12, tzinfo=timezone.utc)


def test_clock_cannot_move_backwards() -> None:
    clock = SimulationClock(_ts(hours=2))
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.set(_ts())


def test_clock_allows_forward_and_identical_sets() -> None:
    clock = SimulationClock(_ts())
    clock.set(_ts())  # identical instant is fine
    clock.set(_ts(minutes=90))
    assert clock.now() == _ts(minutes=90)


def test_clock_time_never_advances_on_its_own() -> None:
    clock = SimulationClock(_ts())
    first = clock.now()
    for _ in range(50):
        assert clock.now() == first
