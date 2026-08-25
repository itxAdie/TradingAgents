"""BrokerAdapter protocol: the ONLY seam through which orders reach a venue.

Rules enforced by structure and by AST tests:
- Nothing outside ``tradingagents/brokers/`` imports a broker SDK.
- The adapter surface has no withdrawal/transfer/funding/account-management
  methods at all — the minimum permission set is market data, account read,
  position read, order submit/cancel/status, reconciliation inputs.
- Errors carry an explicit classification so callers never guess:
  RETRYABLE (safe to retry later), NON_RETRYABLE (never retry),
  UNKNOWN (submission outcome lost — reconcile, never blind-retry).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable


class ConnectionStatus(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    DEGRADED = "DEGRADED"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"


class ErrorClass(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    UNKNOWN = "UNKNOWN"


class BrokerOrderStatus(str, Enum):
    """Broker-reported status normalized onto the canonical lifecycle."""

    PENDING_SUBMIT = "PENDING_SUBMIT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class BrokerError(Exception):
    """Broker failure with explicit retry classification.

    Callers must branch on ``classification``; an ``UNKNOWN`` classification
    after a submission means the outcome is lost and reconciliation — never a
    retry — is the only permitted next step.
    """

    def __init__(
        self,
        classification: ErrorClass,
        code: str,
        message: str,
        broker_raw: str | None = None,
    ) -> None:
        super().__init__(f"[{classification.value}:{code}] {message}")
        self.classification = classification
        self.code = code
        self.message = message
        self.broker_raw = broker_raw  # venue detail, never credentials


@dataclass(frozen=True)
class SubmitOutcome:
    """Result of one submit attempt.

    ``unknown=True`` means the request may or may not have reached the
    venue; the caller must quarantine the order and reconcile. Adapters must
    raise ``BrokerError(classification=UNKNOWN)`` OR return this outcome for
    ambiguous cases — never fabricate a status.
    """

    order: BrokerOrderInfo | None
    unknown: bool = False
    detail: str = ""


@dataclass(frozen=True)
class BrokerOrderInfo:
    broker_order_id: str | None
    client_order_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    order_type: str
    quantity: float
    filled_quantity: float
    avg_fill_price: float | None
    status: BrokerOrderStatus
    fees_reported: float | None  # None => pending, never invented
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss_attached: float | None = None
    take_profit_attached: float | None = None
    ts: datetime | None = None
    raw_status: str = ""  # venue-native status string, preserved verbatim


@dataclass(frozen=True)
class BrokerPosition:
    asset_id: str
    quantity: float  # signed
    avg_entry_price: float
    protective_orders_ok: bool = False
    ts: datetime | None = None


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    account_id: str  # venue-side identity — must match configured account
    currency: str
    cash: float
    equity: float
    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrderInfo, ...]
    server_time: datetime | None
    leverage_cap: float | None = None  # venue maximum; system cap still applies


@runtime_checkable
class BrokerAdapter(Protocol):
    """Every broker implementation must satisfy this contract suite."""

    name: str

    def connect(self) -> ConnectionStatus: ...
    def disconnect(self) -> None: ...
    def health_check(self) -> ConnectionStatus: ...

    def get_account(self) -> BrokerAccountSnapshot: ...
    def get_balances(self) -> dict[str, float]: ...
    def get_positions(self) -> tuple[BrokerPosition, ...]: ...
    def get_orders(self, *, open_only: bool = True) -> tuple[BrokerOrderInfo, ...]: ...
    def find_order(self, *, client_order_id: str) -> BrokerOrderInfo | None: ...

    def submit_order(
        self,
        *,
        client_order_id: str,
        asset_id: str,
        side: Literal["BUY", "SELL"],
        order_type: Literal["MARKET", "LIMIT", "STOP"],
        quantity: float,
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        time_in_force: Literal["GTC", "IOC", "DAY"] = "GTC",
    ) -> SubmitOutcome: ...

    def cancel_order(self, *, client_order_id: str) -> bool: ...

    def modify_order(
        self,
        *,
        client_order_id: str,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> BrokerOrderInfo: ...

    def raw_call_log(self) -> list[dict[str, Any]]:  # test/telemetry hook
        ...
