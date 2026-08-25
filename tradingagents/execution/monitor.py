"""Execution metrics + heartbeats (P5 §43/§48/§74).

A tiny in-process registry: counters, gauges, latency samples, and
component heartbeats. Snapshot feeds /api/system/health and /api/system/
metrics; stale heartbeats are reported as such instead of "healthy".
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()  # reentrant: snapshot() reads via sample_stats()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._samples: dict[str, list[float]] = {}
        self._heartbeats: dict[str, datetime] = {}

    # -- counters -----------------------------------------------------------------

    def inc(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + by

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    # -- gauges ---------------------------------------------------------------------

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def get_gauge(self, name: str) -> float | None:
        with self._lock:
            return self._gauges.get(name)

    # -- latency / slippage samples ------------------------------------------------------

    def record_sample(self, name: str, value_seconds: float) -> None:
        with self._lock:
            samples = self._samples.setdefault(name, [])
            samples.append(value_seconds)
            if len(samples) > 1000:
                del samples[: len(samples) - 1000]

    def sample_stats(self, name: str) -> dict[str, float] | None:
        with self._lock:
            values = self._samples.get(name)
            if not values:
                return None
            ordered = sorted(values)
            n = len(ordered)
            return {
                "count": n,
                "avg": sum(ordered) / n,
                "p50": ordered[n // 2],
                "p95": ordered[min(n - 1, int(n * 0.95))],
                "max": ordered[-1],
            }

    # -- heartbeats -------------------------------------------------------------------------

    def heartbeat(self, component: str, at: datetime | None = None) -> None:
        with self._lock:
            self._heartbeats[component] = at or _utcnow()

    def heartbeat_age(self, component: str, now: datetime | None = None) -> float | None:
        with self._lock:
            beat = self._heartbeats.get(component)
        if beat is None:
            return None
        ref = now or _utcnow()
        return max(0.0, (ref - beat).total_seconds())

    def heartbeat_healthy(self, component: str, *, max_age_seconds: float) -> bool:
        age = self.heartbeat_age(component)
        return age is not None and age <= max_age_seconds

    # -- snapshot ------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "counters": dict(sorted(self._counters.items())),
                "gauges": dict(sorted(self._gauges.items())),
                "latency": {
                    name: stats for name in sorted(self._samples) if (stats := self.sample_stats(name))
                },
                "heartbeats": {
                    name: beat.isoformat() for name, beat in sorted(self._heartbeats.items())
                },
            }
