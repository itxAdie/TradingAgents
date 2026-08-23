"""Deterministic simulation clock.

The backtester's single source of "current time". The research engine (via
its injected ``now_fn``), the replay provider, and the execution simulator
all read from this object, so no component can observe a timestamp beyond
the simulated decision time — the structural guarantee behind the
no-look-ahead rule (ARCHITECTURE.md P2.4).
"""

from __future__ import annotations

from datetime import datetime, timezone


class SimulationClock:
    """Mutable point-in-time holder; advanced explicitly by the runner.

    Invariant: ``now()`` only ever returns UTC-aware datetimes that have been
    explicitly set by the driver. Time never advances on its own.
    """

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("SimulationClock requires a tz-aware datetime")
        self._now = start.astimezone(timezone.utc)
        self._set_count = 0  # determinism telemetry for tests/reports

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        """Move the clock to an explicit instant (must not go backwards)."""
        if moment.tzinfo is None:
            raise ValueError("SimulationClock requires tz-aware datetimes")
        moment = moment.astimezone(timezone.utc)
        if moment < self._now:
            raise ValueError(
                f"simulation clock cannot move backwards "
                f"({self._now.isoformat()} -> {moment.isoformat()})"
            )
        self._now = moment
        self._set_count += 1


def utc(moment: datetime) -> datetime:  # pragma: no cover - trivial helper
    """Normalize any aware datetime to UTC."""
    return moment.astimezone(timezone.utc)


__all__ = ["SimulationClock", "utc"]
