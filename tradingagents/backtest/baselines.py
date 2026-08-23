"""Deterministic baseline strategies (ARCHITECTURE.md P2.6 rationale).

Purpose: answer "does the AI research system add value vs. trivial rules?"
Baselines are deliberately simple, fully deterministic, and emit the *same*
:class:`~tradingagents.research.schemas.ResearchSignal` objects as the AI
path — one signal format everywhere, one executor.

All baselines are long/short symmetric and use ATR-based protective levels
identical to the research assembly methodology (1.5×ATR stop, 2R target) so
comparisons isolate the *decision rule*, not the risk model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tradingagents.analysis.indicators import compute_indicators
from tradingagents.marketdata.models import OhlcvSeries
from tradingagents.research.assembly import REWARD_RISK_RATIO, STOP_ATR_MULTIPLE
from tradingagents.research.schemas import (
    DataSourceRef,
    ResearchSignal,
    RiskLevel,
    SignalAction,
)


class Strategy(Protocol):
    """Anything that can turn point-in-time data into a decision."""

    strategy_id: str

    def generate(
        self, *, asset_id: str, timeframe: str, visible: OhlcvSeries, now: datetime
    ) -> ResearchSignal | None:
        """Return a signal using ONLY ``visible`` data (bars up to now)."""
        ...


def _signal(
    *, strategy_id: str, action: SignalAction, visible: OhlcvSeries, now: datetime,
    thesis: str, snapshot=None,
) -> ResearchSignal:
    close = visible.latest_close or 0.0
    stop = target = None
    risk = RiskLevel.MEDIUM
    if snapshot is not None and snapshot.indicators.get("atr_14"):
        atr = float(snapshot.indicators["atr_14"])
        risk_dist = STOP_ATR_MULTIPLE * atr
        reward_dist = REWARD_RISK_RATIO * risk_dist
        if action is SignalAction.BUY:
            stop, target = close - risk_dist, close + reward_dist
        elif action is SignalAction.SELL:
            stop, target = close + risk_dist, close - reward_dist
        atr_pct = atr / close if close else 0
        risk = (
            RiskLevel.HIGH if atr_pct > 0.03
            else RiskLevel.LOW if atr_pct < 0.012 else RiskLevel.MEDIUM
        )
        target = round(target, 6)
        stop = round(stop, 6)
    return ResearchSignal(
        asset_id=visible.asset_id,
        generated_at=now,
        timeframe=visible.timeframe.value,
        action=action,
        confidence=0.5,  # deterministic rules carry no model confidence
        entry_reference=close or None,
        stop_loss_reference=stop,
        take_profit_reference=target,
        risk_level=risk,
        thesis=thesis,
        supporting_factors=[f"deterministic baseline: {strategy_id}"],
        models_used=[strategy_id],
        data_sources=[
            DataSourceRef(name="replay", kind="market_data", status="historical")
        ],
    )


class BuyAndHoldStrategy:
    """Enter long on the first decision bar; never exits before end-of-data."""

    strategy_id = "baseline_buy_hold"

    def __init__(self) -> None:
        self._entered = False

    def generate(self, *, asset_id, timeframe, visible, now):  # noqa: ANN001
        if self._entered or len(visible.bars) == 0:
            return None
        self._entered = True
        return _signal(
            strategy_id=self.strategy_id, action=SignalAction.BUY,
            visible=visible, now=now,
            thesis="Buy at first opportunity; hold to end of window.",
        )


class SmaCrossStrategy:
    """Long when SMA(fast) crosses above SMA(slow); short on the reverse cross.

    Decisions read only bars available at ``now`` — indicators are recomputed
    on the truncated series every step.
    """

    strategy_id = "baseline_sma_cross"

    def __init__(self, fast: int = 10, slow: int = 30):
        if fast >= slow:
            raise ValueError("fast SMA window must be shorter than slow")
        self.fast = fast
        self.slow = slow

    def generate(self, *, asset_id, timeframe, visible, now):  # noqa: ANN001
        closes = [b.close for b in visible.bars]
        if len(closes) < self.slow + 1:
            return None
        sma = lambda w: sum(closes[-w:]) / w  # noqa: E731 - explicit tiny helper
        prev_closes = closes[:-1]
        fast_now, fast_prev = sma(self.fast), sum(prev_closes[-self.fast:]) / self.fast
        slow_now, slow_prev = sma(self.slow), sum(prev_closes[-self.slow:]) / self.slow
        if fast_prev <= slow_prev and fast_now > slow_now:
            return _signal(
                strategy_id=self.strategy_id, action=SignalAction.BUY,
                visible=visible, now=now,
                thesis=f"SMA{self.fast} crossed above SMA{self.slow} (golden cross).",
            )
        if fast_prev >= slow_prev and fast_now < slow_now:
            return _signal(
                strategy_id=self.strategy_id, action=SignalAction.SELL,
                visible=visible, now=now,
                thesis=f"SMA{self.fast} crossed below SMA{self.slow} (death cross).",
            )
        return None


class MomentumStrategy:
    """Sign of the N-bar return: positive → long, negative → short, zero → HOLD."""

    strategy_id = "baseline_momentum"

    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    def generate(self, *, asset_id, timeframe, visible, now):  # noqa: ANN001
        closes = [b.close for b in visible.bars]
        if len(closes) < self.lookback + 1:
            return None
        ret = closes[-1] / closes[-1 - self.lookback] - 1
        if abs(ret) < 1e-12:
            return None  # HOLD: flat market carries no edge for this rule
        action = SignalAction.BUY if ret > 0 else SignalAction.SELL
        snap = compute_indicators(visible)
        return _signal(
            strategy_id=self.strategy_id, action=action,
            visible=visible, now=now,
            thesis=f"{self.lookback}-bar return {ret:+.4%}; momentum sign rule.",
            snapshot=snap,
        )


__all__ = [
    "BuyAndHoldStrategy",
    "MomentumStrategy",
    "SmaCrossStrategy",
    "Strategy",
]
