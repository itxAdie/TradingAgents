"""Canonical execution-domain models, independent of any broker (P5).

Order states follow the phase-5 state machine exactly: REQUESTED-style
creation flows through SUBMITTED/ACKNOWLEDGED to FILLED, with UNKNOWN as a
first-class state for lost submission responses. Every transition is legal
only along the table below and every transition must be persisted as an
append-only event; the folded record is a derived view, never edited in
place (immutable trade records — corrections are new events).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class LiveOrderState(str, Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


TERMINAL_LIVE_STATES = frozenset(
    {
        LiveOrderState.FILLED,
        LiveOrderState.CANCELLED,
        LiveOrderState.REJECTED,
        LiveOrderState.EXPIRED,
        LiveOrderState.FAILED,
    }
)

#: UNKNOWN is *not* terminal: reconciliation resolves it into a real state.
_LIVE_TRANSITIONS: dict[LiveOrderState, frozenset[LiveOrderState]] = {
    LiveOrderState.CREATED: frozenset({LiveOrderState.RISK_APPROVED, LiveOrderState.REJECTED}),
    LiveOrderState.RISK_APPROVED: frozenset(
        {LiveOrderState.SUBMITTED, LiveOrderState.REJECTED, LiveOrderState.FAILED}
    ),
    LiveOrderState.SUBMITTED: frozenset(
        {
            LiveOrderState.ACKNOWLEDGED,
            LiveOrderState.PARTIALLY_FILLED,
            LiveOrderState.FILLED,
            LiveOrderState.REJECTED,
            LiveOrderState.EXPIRED,
            LiveOrderState.CANCEL_REQUESTED,
            LiveOrderState.UNKNOWN,
            LiveOrderState.FAILED,
        }
    ),
    LiveOrderState.UNKNOWN: frozenset(
        {
            # only reconciliation may leave UNKNOWN — never a blind retry
            LiveOrderState.ACKNOWLEDGED,
            LiveOrderState.PARTIALLY_FILLED,
            LiveOrderState.FILLED,
            LiveOrderState.REJECTED,
            LiveOrderState.EXPIRED,
            LiveOrderState.CANCELLED,
            LiveOrderState.FAILED,
        }
    ),
    LiveOrderState.ACKNOWLEDGED: frozenset(
        {LiveOrderState.PARTIALLY_FILLED, LiveOrderState.FILLED, LiveOrderState.CANCEL_REQUESTED}
    ),
    LiveOrderState.PARTIALLY_FILLED: frozenset(
        {LiveOrderState.PARTIALLY_FILLED, LiveOrderState.FILLED, LiveOrderState.CANCEL_REQUESTED}
    ),
    LiveOrderState.CANCEL_REQUESTED: frozenset(
        {LiveOrderState.CANCELLED, LiveOrderState.PARTIALLY_FILLED, LiveOrderState.FILLED}
    ),
    LiveOrderState.FILLED: frozenset(),
    LiveOrderState.CANCELLED: frozenset(),
    LiveOrderState.REJECTED: frozenset(),
    LiveOrderState.EXPIRED: frozenset(),
    LiveOrderState.FAILED: frozenset(),
}


def live_order_can_transition(current: LiveOrderState, new: LiveOrderState) -> bool:
    return new in _LIVE_TRANSITIONS[current]


class FillRecord(BaseModel):
    """One broker-reported fill. Fees stay pending when not yet reported."""

    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    ts: datetime
    liquidity: Literal["maker", "taker", "unknown"] = "unknown"
    fee: float | None = None
    fee_pending: bool = True
    #: requested reference price at submission time, backend-defined
    reference_price: float | None = None

    @property
    def slippage_bps(self) -> float | None:
        if self.reference_price is None or self.reference_price <= 0:
            return None
        return (self.price - self.reference_price) / self.reference_price * 10_000


class LiveOrder(BaseModel):
    """Current folded view of one live order (never mutated in place)."""

    order_id: str  # internal uuid-ish id, assigned at CREATED
    client_order_id: str  # deterministic idempotency key sent to the broker
    account_id: str
    environment: str  # test|paper|demo|live — frozen from config at creation
    asset_id: str
    timeframe: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP"]
    quantity: float = Field(gt=0)
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: float | None = None  # protective exit level requested broker-side
    take_profit: float | None = None
    #: requested entry reference at submission; slippage baseline for fills
    reference_price: float | None = None
    time_in_force: Literal["GTC", "IOC", "DAY"] = "GTC"
    signal_id: str
    strategy_version: str
    research_version: str
    configuration_version: str
    risk_configuration_version: str
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    filled_at: datetime | None = None
    status: LiveOrderState = LiveOrderState.CREATED
    reason: str = ""  # last transition reason / rejection detail
    broker_order_id: str | None = None
    fills: list[FillRecord] = Field(default_factory=list)
    #: set when a submit attempt's outcome was lost; cleared only by reconcile
    submission_unknown: bool = False
    #: quarantine blocks any retry of this logical order until reconciled
    quarantined: bool = False

    @property
    def filled_quantity(self) -> float:
        return sum(f.quantity for f in self.fills)

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def avg_fill_price(self) -> float | None:
        q = self.filled_quantity
        if q <= 0:
            return None
        return sum(f.price * f.quantity for f in self.fills) / q

    def with_transition(
        self, *, new_state: LiveOrderState, reason: str = "", at: datetime
    ) -> LiveOrder:
        if not live_order_can_transition(self.status, new_state):
            raise ValueError(
                f"illegal live-order transition {self.status.value} -> {new_state.value} "
                f"for {self.order_id}"
            )
        update: dict[str, object] = {"status": new_state, "reason": reason, "updated_at": at}
        if new_state is LiveOrderState.SUBMITTED:
            update["submitted_at"] = at
        elif new_state is LiveOrderState.ACKNOWLEDGED:
            update["acknowledged_at"] = at
        elif new_state is LiveOrderState.FILLED:
            update["filled_at"] = at
            update["submission_unknown"] = False
            update["quarantined"] = False
        elif new_state is LiveOrderState.UNKNOWN:
            update["submission_unknown"] = True
            update["quarantined"] = True
        elif new_state in TERMINAL_LIVE_STATES:
            update["submission_unknown"] = False
            if new_state is not LiveOrderState.FILLED:
                update["quarantined"] = False
        return self.model_copy(update=update)

    def with_fills(self, fill: FillRecord, *, new_state: LiveOrderState, at: datetime) -> LiveOrder:
        """Apply a fill and the resulting state in one immutable update."""
        if new_state not in {LiveOrderState.PARTIALLY_FILLED, LiveOrderState.FILLED}:
            raise ValueError(f"fill must lead to PARTIALLY_FILLED/FILLED, got {new_state}")
        merged = self.model_copy(update={"fills": [*self.fills, fill]})
        return merged.with_transition(new_state=new_state, reason="fill", at=at)


class LiveOrderEvent(BaseModel):
    """Append-only lifecycle line (orders.jsonl)."""

    ts: datetime
    order_id: str
    client_order_id: str
    account_id: str
    signal_id: str
    from_state: LiveOrderState
    to_state: LiveOrderState
    reason: str = ""
    broker_order_id: str | None = None


# -- positions & reconciliation ----------------------------------------------------


class LivePosition(BaseModel):
    """Broker-authoritative position view (adopted after reconciliation)."""

    asset_id: str
    quantity: float  # signed; >0 long, <0 short
    avg_entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    protective_orders_ok: bool = True  # native SL/TP verified present
    updated_at: datetime


class ReconciliationMismatch(BaseModel):
    kind: Literal[
        "MISSING_LOCAL_ORDER",
        "UNEXPECTED_BROKER_ORDER",
        "QUANTITY_MISMATCH",
        "PRICE_MISMATCH",
        "SIDE_MISMATCH",
        "MISSING_LOCAL_POSITION",
        "UNEXPECTED_BROKER_POSITION",
        "POSITION_QUANTITY_MISMATCH",
        "PROTECTIVE_ORDER_MISSING",
        "STALE_STATE",
    ]
    detail: str
    local_value: str | None = None
    broker_value: str | None = None


class ReconciliationReport(BaseModel):
    """Persisted outcome of one reconciliation pass."""

    ts: datetime
    trigger: Literal["startup", "reconnect", "periodic", "post_trade", "post_error", "manual"]
    orders_checked: int
    positions_checked: int
    clean: bool
    mismatches: list[ReconciliationMismatch] = Field(default_factory=list)
    #: actions taken (e.g. "adopted broker qty 0.30 for BTCUSD")
    resolutions: list[str] = Field(default_factory=list)
