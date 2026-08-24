"""In-process event bus bridging engine lifecycle events to SSE subscribers.

The paper engine already emits every state change through the
``NotificationProvider`` protocol (paper/events.py); this module implements
that protocol so ``PaperTradingEngine._emit`` calls flow untouched to web
clients. The bus is thread-safe: producers are engine/worker threads,
consumers are asyncio SSE generators.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

from tradingagents.paper.events import NotificationProvider

_SUBSCRIBER_QUEUE_SIZE = 500
DEFAULT_HISTORY_SIZE = 200


class Subscriber:
    """One SSE consumer with its own bounded queue."""

    def __init__(self) -> None:
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE
        )
        self.dropped = 0

    def put(self, event: dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def get(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Blocking get for worker threads; ``None`` on timeout."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class EventBus(NotificationProvider):
    """Fan-out hub for structured events; keeps a small replay history."""

    name = "event_bus"

    def __init__(self, *, history_size: int = DEFAULT_HISTORY_SIZE):
        self._subs: set[Subscriber] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = threading.Lock()

    # -- NotificationProvider protocol -----------------------------------------

    def notify(self, event: str, payload: dict[str, Any]) -> None:
        self.publish(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "payload": payload,
            }
        )

    # -- direct publishing (API-layer events) ----------------------------------

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault(
            "ts", datetime.now(timezone.utc).isoformat()
        )
        with self._lock:
            self._history.append(event)
            subs = list(self._subs)
        for sub in subs:
            sub.put(event)

    # -- subscription management -------------------------------------------------

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Most recent events, oldest first."""
        with self._lock:
            items = list(self._history)
        return items[-limit:] if limit < len(items) else items


__all__ = ["EventBus", "Subscriber"]
