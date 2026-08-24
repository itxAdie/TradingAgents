"""Paper-trading domain models.

Lifecycle state machines (order + signal), persisted record shapes, and the
bridges to/from the Phase 2 dataclasses (:class:`~tradingagents.backtest.portfolio.Position`,
:class:`~tradingagents.backtest.analytics.EquityPoint`). Every state
transition is validated against an explicit transition table — illegal jumps
raise ``ValueError`` and are never silently applied (ARCHITECTURE.md P3.1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from tradingagents.backtest.analytics import EquityPoint
from tradingagents.backtest.portfolio import Position as SimPosition
from tradingagents.research.schemas import (
    DataSourceRef,
    ResearchSignal,
    RiskLevel,
    SignalAction,
)

# ---------------------------------------------------------------------------
# Order lifecycle


class OrderState(str, Enum):
    SIGNAL = "signal"
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXECUTED = "executed"
    OPEN = "open"
    CLOSED = "closed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.CLOSED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.CANCELLED,
        OrderState.FAILED,
    }
)

_ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.SIGNAL: frozenset({OrderState.PENDING, OrderState.REJECTED}),
    OrderState.PENDING: frozenset(
        {OrderState.ACCEPTED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.CANCELLED}
    ),
    OrderState.ACCEPTED: frozenset(
        {OrderState.EXECUTED, OrderState.EXPIRED, OrderState.CANCELLED}
    ),
    OrderState.EXECUTED: frozenset({OrderState.OPEN, OrderState.FAILED}),
    OrderState.OPEN: frozenset({OrderState.CLOSED}),
    OrderState.CLOSED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.FAILED: frozenset(),
}


def order_can_transition(current: OrderState, new: OrderState) -> bool:
    return new in _ORDER_TRANSITIONS[current]


class PaperOrder(BaseModel):
    """Current view of one paper entry order (folded from transition events)."""

    order_id: str
    account_id: str
    signal_id: str
    asset_id: str
    timeframe: str
    action: Literal["BUY", "SELL"]
    created_at: datetime
    updated_at: datetime
    state: OrderState
    reason: str = ""

    def with_transition(self, *, new_state: OrderState, reason: str, at: datetime) -> PaperOrder:
        if not order_can_transition(self.state, new_state):
            raise ValueError(
                f"illegal order transition {self.state.value} -> {new_state.value} "
                f"for {self.order_id}"
            )
        return self.model_copy(
            update={"state": new_state, "reason": reason, "updated_at": at}
        )


class PaperOrderEvent(BaseModel):
    """One append-only lifecycle transition (orders.jsonl line)."""

    ts: datetime
    order_id: str
    signal_id: str
    account_id: str
    asset_id: str
    timeframe: str
    action: Literal["BUY", "SELL"]
    from_state: OrderState
    to_state: OrderState
    reason: str = ""


def fold_order_events(events: list[PaperOrderEvent]) -> dict[str, PaperOrder]:
    """Replay transition events into current order views (deterministic)."""
    orders: dict[str, PaperOrder] = {}
    for event in events:
        order = orders.get(event.order_id)
        if order is None:
            if event.from_state is not OrderState.SIGNAL:
                raise ValueError(
                    f"first event for {event.order_id} must start from 'signal', "
                    f"got {event.from_state.value}"
                )
            order = PaperOrder(
                order_id=event.order_id,
                account_id=event.account_id,
                signal_id=event.signal_id,
                asset_id=event.asset_id,
                timeframe=event.timeframe,
                action=event.action,
                created_at=event.ts,
                updated_at=event.ts,
                state=OrderState.SIGNAL,
            )
            orders[event.order_id] = order
        orders[event.order_id] = order.with_transition(
            new_state=event.to_state, reason=event.reason, at=event.ts
        )
    return orders


# ---------------------------------------------------------------------------
# Signal lifecycle


class SignalState(str, Enum):
    GENERATED = "generated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


_TERMINAL_SIGNAL_STATES = frozenset(
    {SignalState.REJECTED, SignalState.EXPIRED, SignalState.SUPERSEDED}
)

_SIGNAL_TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.GENERATED: frozenset(
        {
            SignalState.ACCEPTED,
            SignalState.REJECTED,
            SignalState.EXPIRED,
            SignalState.SUPERSEDED,
        }
    ),
    SignalState.ACCEPTED: frozenset({SignalState.EXECUTED, SignalState.EXPIRED}),
    SignalState.EXECUTED: frozenset(),
    SignalState.REJECTED: frozenset(),
    SignalState.EXPIRED: frozenset(),
    SignalState.SUPERSEDED: frozenset(),
}


def signal_can_transition(current: SignalState, new: SignalState) -> bool:
    return new in _SIGNAL_TRANSITIONS[current]


class ResearchSnapshot(BaseModel):
    """Exact AI reasoning captured when the trade/signal was created.

    Stored once, never regenerated later (ARCHITECTURE.md P3.5).
    """

    thesis: str
    bull_case: str = ""
    bear_case: str = ""
    key_factors: list[str] = Field(default_factory=list)
    opposing_factors: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    models_used: list[str] = Field(default_factory=list)
    generated_at: datetime
    market_data_timestamp: datetime | None = None
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    research_version: str  # PROMPT_VERSION of the research stack
    config_hash: str = ""


class PaperSignalRecord(BaseModel):
    """Persisted form of every generated research signal (trade or not)."""

    signal_id: str
    account_id: str
    environment: str
    asset_id: str
    timeframe: str
    state: SignalState
    decision_bar_close: datetime  # effective close of the decision bar
    generated_at: datetime
    market_data_timestamp: datetime | None = None
    action: SignalAction
    confidence: float = Field(ge=0, le=1)
    thesis: str
    supporting_factors: list[str] = Field(default_factory=list)
    opposing_factors: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    entry_reference: float | None = None
    stop_loss_reference: float | None = None
    take_profit_reference: float | None = None
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    research: ResearchSnapshot
    rejection_reason: str = ""
    visible_bars_hash: str = ""
    updated_at: datetime

    def with_transition(self, *, new_state: SignalState, reason: str = "", at: datetime) -> PaperSignalRecord:
        if not signal_can_transition(self.state, new_state):
            raise ValueError(
                f"illegal signal transition {self.state.value} -> {new_state.value} "
                f"for {self.signal_id}"
            )
        return self.model_copy(
            update={
                "state": new_state,
                "rejection_reason": reason or self.rejection_reason,
                "updated_at": at,
            }
        )

    def to_research_signal(self) -> ResearchSignal:
        """Rebuild the Phase 1 signal for the execution simulator."""
        return ResearchSignal(
            asset_id=self.asset_id,
            generated_at=self.generated_at,
            timeframe=self.timeframe,
            action=self.action,
            confidence=self.confidence,
            entry_reference=self.entry_reference,
            stop_loss_reference=self.stop_loss_reference,
            take_profit_reference=self.take_profit_reference,
            risk_level=_risk_level_for(self.confidence),
            thesis=self.thesis,
            supporting_factors=self.supporting_factors,
            opposing_factors=self.opposing_factors,
            invalidation_conditions=self.invalidation_conditions,
            data_sources=self.data_sources,
            models_used=self.models_used,
        )


def _risk_level_for(confidence: float) -> RiskLevel:
    if confidence >= 0.6:
        return RiskLevel.LOW
    if confidence >= 0.35:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Positions / equity / daily performance


class PositionRecord(BaseModel):
    """Persisted open position; bridges to the Phase 2 sim dataclass."""

    position_id: str
    account_id: str
    signal_id: str
    asset_id: str
    timeframe: str
    direction: Literal[1, -1]
    quantity: float = Field(gt=0)
    entry_price: float  # adverse-adjusted fill
    raw_entry_price: float = 0.0
    entry_time: datetime
    updated_at: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    current_price: float | None = None
    strategy_id: str = "ai_research"

    @property
    def unrealized_pnl(self) -> float | None:
        if self.current_price is None:
            return None
        return self.direction * (self.current_price - self.entry_price) * self.quantity

    def to_sim_position(self) -> SimPosition:
        return SimPosition(
            asset_id=self.asset_id,
            direction=self.direction,
            quantity=self.quantity,
            entry_price=self.entry_price,
            entry_time=self.entry_time,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            signal_generated_at=None,  # attribution lives on the record
            strategy_id=self.strategy_id,
            raw_entry_price=self.raw_entry_price,
        )

    @classmethod
    def from_sim_position(
        cls,
        position: SimPosition,
        *,
        account_id: str,
        signal_id: str,
        timeframe: str,
        position_id: str,
        updated_at: datetime,
        current_price: float | None = None,
    ) -> PositionRecord:
        return cls(
            position_id=position_id,
            account_id=account_id,
            signal_id=signal_id,
            asset_id=position.asset_id,
            timeframe=timeframe,
            direction=position.direction,  # type: ignore[arg-type]
            quantity=position.quantity,
            entry_price=position.entry_price,
            raw_entry_price=position.raw_entry_price,
            entry_time=position.entry_time,
            updated_at=updated_at,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            current_price=current_price,
            strategy_id=position.strategy_id,
        )


class EquitySnapshot(EquityPoint):
    """Live equity-curve row: backtest EquityPoint plus accounting detail.

    Subclassing keeps :func:`tradingagents.backtest.analytics.compute_stats`
    directly consumable on paper curves.
    """

    balance: float = 0.0  # settled cash
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


class DailyPerformanceRow(BaseModel):
    """Folded per-UTC-day performance record."""

    date: str  # ISO date (YYYY-MM-DD, UTC)
    starting_equity: float
    ending_equity: float
    daily_return_pct: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    trades_closed: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    fees: float = 0.0
    drawdown_pct: float = 0.0


# ---------------------------------------------------------------------------
# Journal


class JournalNote(BaseModel):
    timestamp: datetime
    author: str = "human"
    text: str = Field(min_length=1)


class JournalEntry(BaseModel):
    """Per-trade journal: exact reasoning snapshot + optional human notes."""

    trade_id: str
    signal_id: str
    account_id: str
    asset_id: str
    timeframe: str
    direction: int
    opened_at: datetime
    closed_at: datetime | None = None
    exit_reason: str = ""
    snapshot: ResearchSnapshot
    trade_summary: dict[str, Any] = Field(default_factory=dict)
    notes: list[JournalNote] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Account state


class AccountState(BaseModel):
    """Persistent virtual-account book (state.json)."""

    schema_version: int = 1
    account_id: str
    environment: str
    initial_capital: float = Field(gt=0)
    cash: float  # settled cash (changes only on close)
    realized_pnl: float = 0.0
    total_costs_paid: float = 0.0
    closed_trades: int = 0
    halted: bool = False
    halt_reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


__all__ = [
    "AccountState",
    "DailyPerformanceRow",
    "EquitySnapshot",
    "JournalEntry",
    "JournalNote",
    "OrderState",
    "PaperOrder",
    "PaperOrderEvent",
    "PaperSignalRecord",
    "PositionRecord",
    "ResearchSnapshot",
    "SignalState",
    "fold_order_events",
    "order_can_transition",
    "signal_can_transition",
]
