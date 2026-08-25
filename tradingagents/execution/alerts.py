"""Execution alert events, emitted via the existing NotificationProvider
protocol (P5 §46) and mirrored onto the SSE EventBus for the dashboard.

No provider is hardcoded: the engine takes whatever NotificationProvider
the caller supplies (logging provider by default).
"""

from __future__ import annotations

from typing import Any

from tradingagents.api.eventbus import EventBus
from tradingagents.paper.events import NotificationProvider

ALERT_BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
ALERT_ORDER_REJECTED = "ORDER_REJECTED"
ALERT_ORDER_UNKNOWN = "ORDER_UNKNOWN"
ALERT_POSITION_MISMATCH = "POSITION_MISMATCH"
ALERT_RISK_LIMIT_REACHED = "RISK_LIMIT_REACHED"
ALERT_CIRCUIT_BREAKER_TRIGGERED = "CIRCUIT_BREAKER_TRIGGERED"
ALERT_EXCESSIVE_SLIPPAGE = "EXCESSIVE_SLIPPAGE"
ALERT_STALE_DATA = "STALE_DATA"
ALERT_DATABASE_FAILURE = "DATABASE_FAILURE"
ALERT_LIVE_TRADING_HALTED = "LIVE_TRADING_HALTED"

KNOWN_ALERTS = (
    ALERT_BROKER_DISCONNECTED,
    ALERT_ORDER_REJECTED,
    ALERT_ORDER_UNKNOWN,
    ALERT_POSITION_MISMATCH,
    ALERT_RISK_LIMIT_REACHED,
    ALERT_CIRCUIT_BREAKER_TRIGGERED,
    ALERT_EXCESSIVE_SLIPPAGE,
    ALERT_STALE_DATA,
    ALERT_DATABASE_FAILURE,
    ALERT_LIVE_TRADING_HALTED,
)


class AlertEmitter:
    def __init__(
        self,
        *,
        notifier: NotificationProvider,
        bus: EventBus | None = None,
        environment_tag: str = "",
    ) -> None:
        self._notifier = notifier
        self._bus = bus
        self._tag = environment_tag

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if event not in KNOWN_ALERTS:
            raise ValueError(f"unknown alert event {event!r}")
        enriched = {"alert": event, "environment": self._tag, **payload}
        self._notifier.notify(event, enriched)
        if self._bus is not None:
            self._bus.publish({"type": "execution_alert", **enriched})
