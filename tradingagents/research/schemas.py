"""Structured research schemas.

Phase 1's primary machine interface. Every agent section is a Pydantic model
(following the existing ``tradingagents.agents.schemas`` conventions — field
descriptions double as output instructions), and the run terminates in a
deterministic :class:`ResearchSignal` that always carries the
``RESEARCH SIGNAL — NOT EXECUTED`` disclaimer as an inescapable literal field.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tradingagents.agents.schemas import SentimentBand  # reuse, no duplication


class SectionStatus(str, Enum):
    """Per-section availability (graceful degradation contract)."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


def _section_note() -> str:
    return (
        "Use only evidence present in the prompt. If data is missing, say so "
        "explicitly; never invent prices, events, or sources."
    )


# ---------------------------------------------------------------------------
# Agent section models
# ---------------------------------------------------------------------------


class TechnicalAnalysis(BaseModel):
    """Structured read of the verified technical snapshot."""

    summary: str = Field(description=(
        "3-6 sentences on price action, trend, momentum and volatility, citing "
        "exact values from the provided snapshot only."
    ))
    trend: Literal["up", "down", "sideways"] = Field(description="Dominant trend view.")
    momentum: Literal["bullish", "bearish", "neutral"] = Field(description="Momentum view.")
    volatility: Literal["low", "medium", "high"] = Field(description="Volatility regime.")
    key_levels: list[str] = Field(default_factory=list, description=(
        "Concrete price levels with one-line justification each, derived from the "
        "snapshot (e.g. 'SMA50 at 2411.2 — dynamic resistance')."
    ))


class MacroAnalysis(BaseModel):
    """Macro/crypto-market context grounded in fetched data (if any)."""

    available: bool = Field(description=(
        "False when no real macro data was provided in the prompt; then leave "
        "summary empty except for what is missing."
    ))
    direction: Literal["supportive_bullish", "supportive_bearish", "neutral"] = Field(
        default="neutral",
        description="Net macro tailwind direction for the asset.",
    )
    summary: str = Field(default="", description=(
        "Only claims supported by the supplied macro/news text. Cite series or "
        "headlines used."
    ))
    factors_considered: list[str] = Field(default_factory=list)
    data_notes: str = Field(default="", description=(
        "Which datasets/headlines were actually available; note gaps honestly."
    ))


class NewsItem(BaseModel):
    headline: str
    source: str | None = None
    published_at: str | None = Field(default=None, description=(
        "Publication time if present in the provided text; otherwise null."
    ))
    relevance: Literal["high", "medium", "low"]
    sentiment: Literal["bullish", "bearish", "neutral"]


class NewsAnalysis(BaseModel):
    available: bool
    tone: Literal["bullish", "bearish", "neutral", "mixed"]
    summary: str = Field(default="", description=_section_note())
    items: list[NewsItem] = Field(default_factory=list, description=(
        "Only items present in the provided news text; never fabricate headlines."
    ))


class SentimentAnalysis(BaseModel):
    """Reuses the existing SentimentBand enum; adds availability + source refs."""

    available: bool
    sources: list[str] = Field(default_factory=list, description=(
        "e.g. ['stocktwits', 'reddit'] — only those that actually returned data."
    ))
    band: SentimentBand | None = None
    score: float | None = Field(default=None, ge=0, le=10)
    summary: str = Field(default="")


class BullCase(BaseModel):
    thesis: str = Field(description=(
        "The strongest honest bullish argument grounded ONLY in the provided "
        "analyst sections."
    ))
    key_points: list[str] = Field(default_factory=list, min_length=2)
    relies_on: list[str] = Field(default_factory=list, description=(
        "Sections/data this case depends on (e.g. 'technical_analysis')."
    ))


class BearCase(BaseModel):
    thesis: str = Field(description=(
        "The strongest honest bearish argument grounded ONLY in the provided "
        "analyst sections."
    ))
    key_points: list[str] = Field(default_factory=list, min_length=2)
    relies_on: list[str] = Field(default_factory=list)


class ResearchManagerVerdict(BaseModel):
    """Weighs bull vs bear and all sections into a research direction."""

    direction: Literal["BUY", "SELL", "HOLD"]
    consensus: str = Field(description=(
        "Which arguments carried the decision and where agents disagreed."
    ))
    self_reported_confidence: float | None = Field(default=None, ge=0, le=1, description=(
        "Your calibrated confidence in this direction [0-1]; null if unsure."
    ))
    invalidation_conditions: list[str] = Field(default_factory=list, description=(
        "Observable conditions that would negate this directional view "
        "(price levels, indicator flips). Only conditions checkable against "
        "provided data."
    ))
    risks: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Run-level artifacts
# ---------------------------------------------------------------------------


class DataSourceRef(BaseModel):
    name: str  # e.g. "yahoo", "fred", "stocktwits"
    kind: Literal["market_data", "news", "sentiment", "macro"]
    status: str  # DataStatus value or "unavailable"
    retrieved_at: datetime | None = None
    detail: str = ""


class AgentFailure(BaseModel):
    agent: str
    error_type: str
    message: str


class ResearchReport(BaseModel):
    asset_id: str
    display_name: str
    timeframe: str
    generated_at: datetime
    market_data_timestamp: datetime | None
    market_data_status: str
    technical_analysis: TechnicalAnalysis | None = None
    macro_analysis: MacroAnalysis | None = None
    news_analysis: NewsAnalysis | None = None
    sentiment_analysis: SentimentAnalysis | None = None
    bull_case: BullCase | None = None
    bear_case: BearCase | None = None
    manager_verdict: ResearchManagerVerdict | None = None
    confidence: float = Field(ge=0, le=1)
    confidence_breakdown: dict[str, float] = Field(default_factory=dict)
    confidence_method: str = (
        "experimental heuristic: weighted agent_agreement(0.40)/"
        "data_completeness(0.25)/signal_consistency(0.20)/model_confidence(0.15)"
    )
    risks: list[str] = Field(default_factory=list)
    agent_failures: list[AgentFailure] = Field(default_factory=list)
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    disclaimer: str = "RESEARCH SIGNAL — NOT EXECUTED"


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ResearchSignal(BaseModel):
    """Deterministic research output. Never executes anything anywhere."""

    asset_id: str
    generated_at: datetime
    timeframe: str
    action: SignalAction
    confidence: float = Field(ge=0, le=1)
    entry_reference: float | None = None
    stop_loss_reference: float | None = None
    take_profit_reference: float | None = None
    risk_level: RiskLevel
    thesis: str
    supporting_factors: list[str] = Field(default_factory=list)
    opposing_factors: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    signal_kind: Literal["research"] = Field(
        default="research",
        description="Always 'research' — distinguishes from future execution signals.",
    )
    disclaimer: Literal["RESEARCH SIGNAL — NOT EXECUTED"] = Field(
        default="RESEARCH SIGNAL — NOT EXECUTED",
        description="Immutable label; this signal must never be treated as an order.",
    )

    @field_validator("confidence")
    @classmethod
    def _round_confidence(cls, v: float) -> float:
        return round(float(v), 4)


# ---------------------------------------------------------------------------
# Renderers (presentation layer reads these, not raw dicts)
# ---------------------------------------------------------------------------


def render_signal(signal: ResearchSignal) -> str:
    """Human-facing block matching the brief's RESEARCH OUTPUT example."""
    pct = f"{round(signal.confidence * 100)}%"
    lines = [
        f"Asset:\n  {signal.asset_id}",
        f"Time:\n  {signal.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Timeframe:\n  {signal.timeframe}",
        f"Signal:\n  {signal.action.value}",
        f"Confidence:\n  {pct} (experimental heuristic)",
        f"Entry Reference:\n  {_fmt(signal.entry_reference)}",
        f"Stop Reference:\n  {_fmt(signal.stop_loss_reference)}",
        f"Target Reference:\n  {_fmt(signal.take_profit_reference)}",
        f"Risk:\n  {signal.risk_level.value}",
        "",
        f"Bull Case / Thesis:\n  {signal.thesis}",
    ]
    lines += ["Key Factors:"]
    lines += [f"  - {f}" for f in signal.supporting_factors] or ["  - n/a"]
    if signal.opposing_factors:
        lines += ["Opposing Factors:"]
        lines += [f"  - {f}" for f in signal.opposing_factors]
    if signal.invalidation_conditions:
        lines += ["Invalidation:"]
        lines += [f"  - {c}" for c in signal.invalidation_conditions]
    srcs = ", ".join(sorted({s.name for s in signal.data_sources})) or "n/a"
    models = ", ".join(signal.models_used) or "n/a"
    lines += [
        "",
        f"Data Sources:\n  {srcs}",
        f"AI Models:\n  {models}",
        "",
        signal.disclaimer,
    ]
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")
