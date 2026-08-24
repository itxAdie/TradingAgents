"""Event names + notification abstraction.

Events flow through :func:`tradingagents.research.logging.log_event` (JSON
lines, secret-filtered) via the default ``LoggingNotificationProvider``. The
``NotificationProvider`` protocol exists so Telegram/Discord/webhook adapters
can be added later without touching the engine — no event framework is
introduced (PROJECT_RULES #2, ARCHITECTURE.md P3.1).
"""

from __future__ import annotations

from typing import Any, Protocol

from tradingagents.research.logging import log_event

EVENT_MARKET_DATA_UPDATED = "paper_market_data_updated"
EVENT_NO_NEW_BAR = "paper_no_new_bar"
EVENT_RESEARCH_FAILED = "paper_research_failed"
EVENT_SIGNAL_NOT_GENERATED = "paper_signal_not_generated"
EVENT_SIGNAL_GENERATED = "paper_signal_generated"
EVENT_DUPLICATE_SUPPRESSED = "paper_duplicate_signal_suppressed"
EVENT_VALIDATION_REJECTED = "paper_signal_validation_rejected"
EVENT_RISK_REJECTED = "paper_risk_rejected"
EVENT_SIGNAL_ACCEPTED = "paper_signal_accepted"
EVENT_ORDER_STATE = "paper_order_state_changed"
EVENT_POSITION_OPENED = "paper_position_opened"
EVENT_POSITION_CLOSED = "paper_position_closed"
EVENT_TRADE_CLOSED = "paper_trade_recorded"
EVENT_STOP_LOSS_TRIGGERED = "paper_stop_loss_triggered"
EVENT_TAKE_PROFIT_TRIGGERED = "paper_take_profit_triggered"
EVENT_EQUITY_SNAPSHOT = "paper_equity_snapshot"
EVENT_TRADING_DISABLED = "paper_trading_disabled"
EVENT_EMERGENCY_HALT = "paper_emergency_halt"
EVENT_CYCLE_FAILED = "paper_cycle_failed"
EVENT_RECOVERY_LOADED = "paper_recovery_loaded"
EVENT_PENDING_EXPIRED = "paper_pending_expired"


class NotificationProvider(Protocol):
    """Minimal notification seam; logging implementation ships by default."""

    name: str

    def notify(self, event: str, payload: dict[str, Any]) -> None:
        """Deliver one structured event."""
        ...


class LoggingNotificationProvider:
    """Default provider: structured JSON log lines, secrets filtered."""

    name = "logging"

    def notify(self, event: str, payload: dict[str, Any]) -> None:
        log_event(event, **payload)


__all__ = [
    "LoggingNotificationProvider",
    "NotificationProvider",
    # event names
    "EVENT_CYCLE_FAILED",
    "EVENT_DUPLICATE_SUPPRESSED",
    "EVENT_EQUITY_SNAPSHOT",
    "EVENT_EMERGENCY_HALT",
    "EVENT_MARKET_DATA_UPDATED",
    "EVENT_NO_NEW_BAR",
    "EVENT_ORDER_STATE",
    "EVENT_PENDING_EXPIRED",
    "EVENT_POSITION_CLOSED",
    "EVENT_POSITION_OPENED",
    "EVENT_RECOVERY_LOADED",
    "EVENT_RISK_REJECTED",
    "EVENT_SIGNAL_ACCEPTED",
    "EVENT_SIGNAL_GENERATED",
    "EVENT_SIGNAL_NOT_GENERATED",
    "EVENT_STOP_LOSS_TRIGGERED",
    "EVENT_TAKE_PROFIT_TRIGGERED",
    "EVENT_TRADE_CLOSED",
    "EVENT_TRADING_DISABLED",
    "EVENT_VALIDATION_REJECTED",
]
