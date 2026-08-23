"""Unit tests: deterministic indicator engine."""

from __future__ import annotations

import math

import pytest

from tests._research_factories import make_series
from tradingagents.analysis.indicators import (
    TechnicalSnapshot,
    classify_momentum,
    classify_trend,
    classify_volatility,
    compute_indicators,
    render_snapshot,
)


@pytest.mark.unit
class TestClassifiers:
    def test_trend_alignment(self):
        assert classify_trend(110, 100, 90) == "up"
        assert classify_trend(80, 100, 90) == "down"
        assert classify_trend(95, 100, 90) == "sideways"
        assert classify_trend(None, 100, None) == "unknown"

    def test_momentum_prefers_macd_hist(self):
        assert classify_momentum(0.5, None) == "bullish"
        assert classify_momentum(-0.5, 70.0) == "bearish"  # macd wins over rsi
        assert classify_momentum(0.0, 70) == "neutral"
        assert classify_momentum(None, 70) == "bullish"
        assert classify_momentum(None, None) == "unknown"

    def test_volatility_buckets(self):
        assert classify_volatility(3.5, 100) == "high"   # 3.5%
        assert classify_volatility(2.0, 100) == "medium"  # 2%
        assert classify_volatility(0.5, 100) == "low"    # 0.5%
        assert classify_volatility(None, 100) == "unknown"


@pytest.mark.unit
class TestComputeIndicators:
    def test_full_lookback_populates_indicators(self):
        series = make_series(260, drift_pct_per_bar=0.05)
        snap = compute_indicators(series)
        assert isinstance(snap, TechnicalSnapshot)
        for name in ("rsi_14", "macd", "macd_signal", "sma_50", "sma_200",
                     "ema_10", "boll_upper", "boll_lower", "atr_14"):
            value = snap.indicators[name]
            assert value is not None, f"{name} should be computable with 260 bars"
            assert not (isinstance(value, float) and math.isnan(value))
        # Rising market: trend must be up and momentum bullish.
        assert snap.trend == "up"
        assert snap.momentum == "bullish"

    def test_sma200_matches_manual_computation(self):
        series = make_series(260, base_price=50.0)
        snap = compute_indicators(series)
        closes = [b.close for b in series.bars][-200:]
        expected = sum(closes) / len(closes)
        assert abs(snap.indicators["sma_200"] - expected) < 1e-6

    def test_insufficient_lookback_explicit_not_nan(self):
        series = make_series(60)
        snap = compute_indicators(series)
        assert snap.indicators["sma_200"] is None
        assert "insufficient lookback" in snap.missing_reasons["sma_200"]
        # Shorter indicators still computed.
        assert snap.indicators["ema_10"] is not None

    def test_empty_series_degrades(self):
        snap = compute_indicators(make_series(0))
        assert snap.bar_count == 0
        assert "*" in snap.missing_reasons

    def test_deterministic(self):
        s1 = compute_indicators(make_series(120, drift_pct_per_bar=0.01))
        s2 = compute_indicators(make_series(120, drift_pct_per_bar=0.01))
        assert s1.indicators == s2.indicators
        assert s1.trend == s2.trend

    def test_render_contains_numbers_and_disclaimer_note(self):
        text = render_snapshot(compute_indicators(make_series(260)))
        assert "Verified technical snapshot" in text
        assert "rsi_14" in text
        assert "unavailable" in text or "|" in text


@pytest.mark.unit
def test_crypto_series_uses_crypto_annualization():

    from tests._research_factories import utc_now
    series = make_series(
        150,
        asset_id="BTCUSD",
        end=utc_now(),
        hours_step=24,
        zigzag_pct_per_bar=0.4,
    )
    snap = compute_indicators(series)
    vol = snap.indicators["realized_volatility_annualized"]
    assert vol is not None and vol > 0
