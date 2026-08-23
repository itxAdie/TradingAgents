"""Deterministic assembly of :class:`ResearchSignal` from verified inputs.

The LLM decides *direction* (via the Research Manager); everything numeric
here — reference levels, risk bucket, confidence components, invalidation
checks — is computed deterministically so a signal can always be audited:

- Entry reference = latest verified close (never an LLM-invented number).
- Stop / target = ATR-scaled bands around that close (constants below), only
  emitted for directional actions and only when ATR is available.
- LLM-suggested levels are intentionally NOT used: the brief forbids
  unverified numbers in the signal.

If market data is unusable the assembler returns ``None`` — no signal is ever
produced from stale/unknown-age data.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError as PydValidationError

from tradingagents.analysis import confidence as conf
from tradingagents.analysis.indicators import (
    ATR_PCT_HIGH,
    ATR_PCT_MEDIUM,
    TechnicalSnapshot,
)
from tradingagents.assets.registry import AssetSpec
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.research.schemas import (
    AgentFailure,
    BearCase,
    BullCase,
    DataSourceRef,
    MacroAnalysis,
    NewsAnalysis,
    ResearchManagerVerdict,
    ResearchReport,
    ResearchSignal,
    RiskLevel,
    SentimentAnalysis,
    SignalAction,
    TechnicalAnalysis,
)

# ATR multiples for protective levels (documented methodology).
STOP_ATR_MULTIPLE = 1.5
REWARD_RISK_RATIO = 2.0


class AssembledResult:
    """Signal plus its report context, or an explicit no-signal outcome."""

    def __init__(
        self,
        report: ResearchReport,
        signal: ResearchSignal | None,
        reason: str = "",
    ):
        self.report = report
        self.signal = signal
        self.no_signal_reason = reason


def _risk_level(snapshot: TechnicalSnapshot) -> RiskLevel:
    atr = snapshot.indicators.get("atr_14")
    close = snapshot.latest_close
    if atr is None or not close:
        # Unknown volatility must not silently read as low risk.
        return RiskLevel.MEDIUM
    atr_pct = atr / close
    if atr_pct >= ATR_PCT_HIGH:
        return RiskLevel.HIGH
    if atr_pct >= ATR_PCT_MEDIUM:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _directional_levels(
    action: SignalAction, close: float, atr: float | None
) -> tuple[float | None, float | None, float | None]:
    """(entry, stop, target) derived from verified close + ATR."""
    if action is SignalAction.HOLD or atr is None:
        return close, None, None
    risk = STOP_ATR_MULTIPLE * atr
    reward = REWARD_RISK_RATIO * risk
    if action is SignalAction.BUY:
        return close, close - risk, close + reward
    return close, close + risk, close - reward


def _deterministic_action(snapshot: TechnicalSnapshot) -> SignalAction:
    if snapshot.trend == "up" and snapshot.momentum == "bullish":
        return SignalAction.BUY
    if snapshot.trend == "down" and snapshot.momentum == "bearish":
        return SignalAction.SELL
    return SignalAction.HOLD


def assemble(
    *,
    asset: AssetSpec,
    timeframe: Timeframe,
    generated_at: datetime,
    snapshot: TechnicalSnapshot | None,
    technical: TechnicalAnalysis | None,
    macro: MacroAnalysis | None,
    news: NewsAnalysis | None,
    sentiment: SentimentAnalysis | None,
    bull: BullCase | None,
    bear: BearCase | None,
    verdict: ResearchManagerVerdict | None,
    failures: list[AgentFailure],
    data_sources: list[DataSourceRef],
    models_used: list[str],
) -> tuple[ResearchSignal | None, str]:
    """Build the final signal. Returns ``(signal, no_signal_reason)``.

    ``signal`` is ``None`` (with a reason) whenever verified market data is
    absent — the system never emits a signal from unknown-age data.
    """
    if snapshot is None or snapshot.bar_count == 0 or snapshot.latest_close is None:
        return None, "no verified market data; refusing to emit a signal"

    action = (
        SignalAction(verdict.direction)
        if verdict is not None
        else _deterministic_action(snapshot)
    )

    atr = snapshot.indicators.get("atr_14")
    entry, stop, target = _directional_levels(action, snapshot.latest_close, atr)

    breakdown = conf.compute_confidence(
        action.value,
        trend=snapshot.trend if snapshot.trend != "unknown" else None,
        momentum=snapshot.momentum if snapshot.momentum != "unknown" else None,
        macro_direction=macro.direction if macro and macro.available else None,
        news_tone=news.tone if news and news.available else None,
        sentiment_band=str(sentiment.band.value) if sentiment and sentiment.available and sentiment.band else None,
        manager_direction=verdict.direction if verdict else None,
        news_available=bool(news and news.available),
        sentiment_available=bool(sentiment and sentiment.available),
        macro_available=bool(macro and macro.available),
        model_confidences=(
            [verdict.self_reported_confidence]
            if verdict and verdict.self_reported_confidence is not None
            else []
        ),
    )

    supporting: list[str] = []
    opposing: list[str] = []
    trend_txt = f"deterministic trend={snapshot.trend}, momentum={snapshot.momentum}"
    # The trend line supports the action when the stated direction agrees with
    # it (or the action is HOLD and a trend exists to contextualize).
    trend_agrees = (
        (action is SignalAction.BUY and snapshot.trend == "up")
        or (action is SignalAction.SELL and snapshot.trend == "down")
        or action is SignalAction.HOLD
    )
    (supporting if trend_agrees else opposing).append(trend_txt)

    if bull and bull.key_points:
        supporting.append(f"Bull case: {bull.key_points[0]}")
    if bear and bear.key_points:
        opposing.append(f"Bear case: {bear.key_points[0]}")
    if news and news.available:
        (supporting if news.tone == "bullish" else opposing).append(
            f"news tone={news.tone} ({len(news.items)} items)"
        )
    if sentiment and sentiment.available and sentiment.band:
        band = sentiment.band.value
        (supporting if "Bullish" in band else opposing).append(f"sentiment={band}")
    if macro and macro.available:
        direction_txt = f"macro direction={macro.direction}"
        (supporting if macro.direction == "supportive_bullish" else opposing).append(direction_txt)
    for failure in failures:
        opposing.append(f"{failure.agent} unavailable: {failure.error_type}")

    invalidations: list[str] = []
    if stop is not None:
        side = "below" if action is SignalAction.BUY else "above"
        invalidations.append(
            f"close {side} stop reference {stop:.4f} invalidates this research view"
        )
    sma50 = snapshot.indicators.get("sma_50")
    if action is SignalAction.BUY and sma50 is not None:
        invalidations.append(f"sustained closes below SMA50 ({sma50:.4f}) negate the up-regime")
    if action is SignalAction.SELL and sma50 is not None:
        invalidations.append(f"sustained closes above SMA50 ({sma50:.4f}) negate the down-regime")
    if action is SignalAction.HOLD:
        invalidations.append(
            "regime flip: decisive trend+momentum alignment would replace HOLD"
        )
    if verdict:
        invalidations.extend(verdict.invalidation_conditions)

    thesis = verdict.consensus if verdict else (
        f"Deterministic fallback: {snapshot.trend} trend with "
        f"{snapshot.momentum} momentum on {asset.display_name} "
        f"@ {timeframe.value}."
    )

    try:
        signal = ResearchSignal(
            asset_id=asset.asset_id,
            generated_at=generated_at,
            timeframe=timeframe.value,
            action=action,
            confidence=breakdown.score,
            entry_reference=entry,
            stop_loss_reference=stop,
            take_profit_reference=target,
            risk_level=_risk_level(snapshot),
            thesis=thesis,
            supporting_factors=supporting,
            opposing_factors=opposing,
            invalidation_conditions=invalidations,
            data_sources=data_sources,
            models_used=models_used,
        )
    except PydValidationError as exc:  # defensive: schema drift must be loud
        raise ValueError(f"assembled signal failed validation: {exc}") from exc
    return signal, ""


__all__ = [
    "AssembledResult",
    "assemble",
    "REWARD_RISK_RATIO",
    "STOP_ATR_MULTIPLE",
]
