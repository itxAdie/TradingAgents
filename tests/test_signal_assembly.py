"""Unit tests: deterministic signal assembly."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradingagents.analysis.indicators import TechnicalSnapshot
from tradingagents.assets.registry import get_asset
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.research.assembly import (
    REWARD_RISK_RATIO,
    STOP_ATR_MULTIPLE,
    assemble,
)
from tradingagents.research.schemas import (
    AgentFailure,
    ResearchManagerVerdict,
    RiskLevel,
    SignalAction,
)

GOLD = get_asset("XAUUSD")
NOW = datetime.now(timezone.utc)


def _snapshot(**indicator_overrides) -> TechnicalSnapshot:
    indicators = {
        "ema_10": 2405.0, "sma_20": 2398.0, "sma_50": 2390.0, "sma_200": 2350.0,
        "rsi_14": 62.0, "macd": 3.1, "macd_signal": 2.4, "macd_hist": 0.7,
        "boll_mid": 2399.0, "boll_upper": 2420.0, "boll_lower": 2378.0,
        "atr_14": 12.0, "momentum_10_pct": 0.8,
        "realized_volatility_annualized": 0.15,
    }
    indicators.update(indicator_overrides)
    return TechnicalSnapshot(
        asset_id="XAUUSD", timeframe=Timeframe.H1, computed_at=NOW,
        bar_count=300, first_bar_timestamp=NOW, latest_close=2400.0,
        latest_bar_timestamp=NOW, indicators=indicators, missing_reasons={},
        trend="up", momentum="bullish", volatility="medium",
    )


def _sideways_snapshot() -> TechnicalSnapshot:
    snap = _snapshot().model_copy()
    object.__setattr__(snap, "trend", "sideways")
    object.__setattr__(snap, "momentum", "neutral")
    return snap


def _verdict(direction="BUY") -> ResearchManagerVerdict:
    return ResearchManagerVerdict(
        direction=direction, consensus="test",
        self_reported_confidence=0.8,
        invalidation_conditions=["manager-said condition"],
        risks=["risk-one"],
    )


@pytest.mark.unit
class TestAssembly:
    def test_buy_signal_levels_are_atr_derived(self):
        snap = _snapshot()
        signal, reason = assemble(
            asset=GOLD, timeframe=Timeframe.H1, generated_at=NOW, snapshot=snap,
            technical=None, macro=None, news=None, sentiment=None, bull=None,
            bear=None, verdict=_verdict("BUY"), failures=[],
            data_sources=[], models_used=["m"],
        )
        assert reason == ""
        assert signal.action is SignalAction.BUY
        assert signal.entry_reference == pytest.approx(2400.0)
        risk = STOP_ATR_MULTIPLE * 12.0
        assert signal.stop_loss_reference == pytest.approx(2400.0 - risk)
        assert signal.take_profit_reference == pytest.approx(
            2400.0 + REWARD_RISK_RATIO * risk
        )
        # Invalidation includes deterministic stop breach + manager condition.
        assert any("stop" in c for c in signal.invalidation_conditions)
        assert "manager-said condition" in signal.invalidation_conditions

    def test_hold_has_no_protective_levels(self):
        snap = _sideways_snapshot()
        signal, _ = assemble(
            asset=GOLD, timeframe=Timeframe.H1, generated_at=NOW, snapshot=snap,
            technical=None, macro=None, news=None, sentiment=None, bull=None,
            bear=None, verdict=_verdict("HOLD"), failures=[],
            data_sources=[], models_used=[],
        )
        assert signal.action is SignalAction.HOLD
        assert signal.entry_reference == pytest.approx(2400.0)
        assert signal.stop_loss_reference is None
        assert signal.take_profit_reference is None

    def test_no_snapshot_means_no_signal(self):
        signal, reason = assemble(
            asset=GOLD, timeframe=Timeframe.H1, generated_at=NOW, snapshot=None,
            technical=None, macro=None, news=None, sentiment=None, bull=None,
            bear=None, verdict=_verdict(), failures=[], data_sources=[], models_used=[],
        )
        assert signal is None
        assert "refusing" in reason

    @pytest.mark.parametrize("atr_pct,risk", [
        (0.05, RiskLevel.HIGH),
        (0.02, RiskLevel.MEDIUM),
        (0.005, RiskLevel.LOW),
    ])
    def test_risk_level_buckets(self, atr_pct, risk):
        snap = _snapshot(atr_14=2400.0 * atr_pct)
        signal, _ = assemble(
            asset=GOLD, timeframe=Timeframe.H1, generated_at=NOW, snapshot=snap,
            technical=None, macro=None, news=None, sentiment=None, bull=None,
            bear=None, verdict=_verdict(), failures=[], data_sources=[], models_used=[],
        )
        assert signal.risk_level is risk

    def test_deterministic_fallback_without_verdict(self):
        snap = _snapshot()
        signal, _ = assemble(
            asset=GOLD, timeframe=Timeframe.H1, generated_at=NOW, snapshot=snap,
            technical=None, macro=None, news=None, sentiment=None, bull=None,
            bear=None, verdict=None, failures=[],
            data_sources=[], models_used=[],
        )
        # up trend + bullish momentum -> BUY even without an LLM verdict
        assert signal.action is SignalAction.BUY

    def test_agent_failure_recorded_as_opposing_factor(self):
        snap = _snapshot()
        failure = AgentFailure(agent="news_analyst", error_type="RuntimeError", message="x")
        signal, _ = assemble(
            asset=GOLD, timeframe=Timeframe.H1, generated_at=NOW, snapshot=snap,
            technical=None, macro=None, news=None, sentiment=None, bull=None,
            bear=None, verdict=_verdict(), failures=[failure],
            data_sources=[], models_used=[],
        )
        assert any("news_analyst" in f for f in signal.opposing_factors)
