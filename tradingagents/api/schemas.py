"""Typed API response contracts (spec §26).

Every route returns Pydantic models — either existing domain models verbatim
(they are already JSON-first and carry disclaimers) or the explicit
projection models below. List endpoints use the standard ``Page`` envelope;
errors use the standard ``ErrorEnvelope``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from tradingagents.api.backtests import BacktestStartRequest
from tradingagents.backtest.analytics import PerformanceStats
from tradingagents.paper.models import (
    DailyPerformanceRow,
    EquitySnapshot,
    JournalEntry,
    PaperOrder,
    PaperSignalRecord,
)

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Standard list envelope with server-side pagination metadata."""

    items: list[T]
    total: int
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------------------
# Markets


class QuoteOut(BaseModel):
    asset_id: str
    timestamp: datetime
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    source: str
    data_status: str


class AssetSummary(BaseModel):
    asset_id: str
    display_name: str
    asset_class: str
    quote_currency: str


class MarketOverviewItem(BaseModel):
    spec: AssetSummary
    quote: QuoteOut | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    change_timeframe: str | None = None
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    note: str = ""  # provider failure explanation when quote is None


class CandleOut(BaseModel):
    t: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class CandlesResponse(BaseModel):
    asset_id: str
    timeframe: str
    source: str
    data_status: str
    freshness: Literal["fresh", "stale", "unknown"]
    bars: list[CandleOut]


class IndicatorSeries(BaseModel):
    name: str
    values: list[float | None]


class IndicatorsResponse(BaseModel):
    asset_id: str
    timeframe: str
    timestamps: list[datetime]
    series: list[IndicatorSeries]
    na_reasons: dict[str, str] = Field(default_factory=dict)


class ScheduleSlotView(BaseModel):
    asset_id: str
    timeframe: str
    enabled: bool
    next_run_at: str | None = None
    last_processed_bar_close: str | None = None


class MarketDetailResponse(MarketOverviewItem):
    latest_research_run: ResearchRunRef | None = None  # noqa: F821 (forward)
    latest_signal_ref: SignalRef | None = None  # noqa: F821 (forward)
    scheduled_slots: list[ScheduleSlotView] = Field(default_factory=list)


class ResearchRunRef(BaseModel):
    run_id: str
    generated_at: datetime
    signal_action: str | None = None
    confidence: float | None = None


class SignalRef(BaseModel):
    signal_id: str
    state: str
    action: str
    confidence: float
    generated_at: datetime


MarketDetailResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Paper trading


class PositionOut(BaseModel):
    position_id: str
    account_id: str
    signal_id: str
    asset_id: str
    timeframe: str
    direction: int
    quantity: float
    entry_price: float
    raw_entry_price: float
    entry_time: datetime
    updated_at: datetime
    stop_loss: float | None
    take_profit: float | None
    current_price: float | None
    unrealized_pnl: float | None
    strategy_id: str


class PortfolioResponse(BaseModel):
    schema_name: str = "PAPER_ACCOUNT_REPORT"
    account_id: str
    environment: str
    generated_at: datetime
    halted: bool
    halt_reason: str
    initial_capital: float
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    total_costs_paid: float
    total_return_pct: float
    open_positions: list[Any]
    stats: PerformanceStats
    daily: list[DailyPerformanceRow]
    disclaimer: str
    orders_total: int = 0
    open_orders: list[PaperOrder] = Field(default_factory=list)


class EquityCurveResponse(Page[EquitySnapshot]):
    pass


class DailyRowsResponse(Page[DailyPerformanceRow]):
    pass


class PositionsResponse(Page[PositionOut]):
    pass


# ---------------------------------------------------------------------------
# Signals / trades / journal


class SignalTransitionRow(BaseModel):
    signal_id: str
    from_state: str
    to_state: str
    reason: str = ""
    ts: datetime | None = None


class SignalListItem(BaseModel):
    signal_id: str
    asset_id: str
    timeframe: str
    state: str
    action: str
    confidence: float
    generated_at: datetime
    updated_at: datetime
    rejection_reason: str = ""
    executed: bool = False
    risk_decision: str = ""  # approved | rejected:<code> | "" (pending)


class SignalListPage(Page[SignalListItem]):
    pass


class SignalDetailResponse(BaseModel):
    record: PaperSignalRecord
    transitions: list[SignalTransitionRow]
    orders: list[PaperOrder]
    research_run: ResearchRunRef | None = None


class TradeListItem(BaseModel):
    trade_id: str
    run_id: str
    asset_id: str
    timeframe: str
    direction: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_price: float
    exit_price: float
    net_pnl: float
    return_pct: float
    holding_period: str
    bars_held: int
    outcome: Literal["win", "loss"]
    exit_reason: str
    has_journal: bool
    strategy_version: str = ""  # research_version from the journal snapshot


class TradeListPage(Page[TradeListItem]):
    pass


class TimelineStage(BaseModel):
    stage: str
    label: str
    timestamp: datetime | None
    detail: str = ""


class TradeDetailResponse(BaseModel):
    trade: dict[str, Any]  # TradeRecord fields verbatim
    journal: JournalEntry | None = None
    timeline: list[TimelineStage]
    related_signal: PaperSignalRecord | None = None


class JournalNoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    author: str = Field(default="human", max_length=64)


class AuditEventRow(BaseModel):
    ts: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditLogResponse(Page[AuditEventRow]):
    pass


# ---------------------------------------------------------------------------
# Risk


class RiskLimitValue(BaseModel):
    key: str
    label: str
    limit_value: float
    current_value: float
    utilization_pct: float  # 0-100+, backend-computed only
    unit: Literal["pct", "count", "currency"]


class RiskStatusResponse(BaseModel):
    environment: str
    account_id: str
    halted: bool
    halt_reason: str
    equity: float
    day_start_equity: float | None
    peak_equity: float | None
    gross_exposure: float
    open_positions: int
    limits: list[RiskLimitValue]


class RiskEventItem(BaseModel):
    ts: datetime | None
    type: str
    asset_id: str = ""
    message: str
    ref_id: str = ""


class RiskEventsPage(Page[RiskEventItem]):
    pass


# ---------------------------------------------------------------------------
# Backtests / system / events


class BacktestJobOut(BaseModel):
    run_id: str
    status: str
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    params: BacktestStartRequest


class SystemStatusItem(BaseModel):
    component: str
    status: Literal["online", "offline", "degraded", "enabled", "disabled", "idle"]
    detail: str = ""


class SystemStatusResponse(BaseModel):
    overall: Literal["online", "degraded", "offline"]
    components: list[SystemStatusItem]
    generated_at: datetime


class EventRow(BaseModel):
    ts: str
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventHistoryPage(Page[EventRow]):
    pass


__all__ = [
    "AssetSummary",
    "AuditEventRow",
    "AuditLogResponse",
    "BacktestJobOut",
    "CandleOut",
    "CandlesResponse",
    "DailyRowsResponse",
    "EquityCurveResponse",
    "ErrorBody",
    "ErrorEnvelope",
    "EventHistoryPage",
    "EventRow",
    "IndicatorSeries",
    "IndicatorsResponse",
    "JournalNoteIn",
    "MarketDetailResponse",
    "MarketOverviewItem",
    "Page",
    "PortfolioResponse",
    "PositionOut",
    "PositionsResponse",
    "QuoteOut",
    "ResearchRunRef",
    "RiskEventItem",
    "RiskEventsPage",
    "RiskLimitValue",
    "RiskStatusResponse",
    "ScheduleSlotView",
    "SignalDetailResponse",
    "SignalListItem",
    "SignalListPage",
    "SignalRef",
    "SystemStatusItem",
    "SystemStatusResponse",
    "TimelineStage",
    "TradeDetailResponse",
    "TradeListItem",
    "TradeListPage",
]
