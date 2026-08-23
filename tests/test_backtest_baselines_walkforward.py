"""Phase 2 — baseline strategies and walk-forward windowing/aggregation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests._research_factories import make_series
from tradingagents.backtest.baselines import (
    BuyAndHoldStrategy,
    MomentumStrategy,
    SmaCrossStrategy,
)
from tradingagents.backtest.walkforward import (
    WalkForwardConfig,
    aggregate_window_results,
    generate_windows,
)
from tradingagents.marketdata.models import DataStatus, OhlcvSeries
from tradingagents.research.assembly import REWARD_RISK_RATIO, STOP_ATR_MULTIPLE
from tradingagents.research.schemas import SignalAction

UTC = timezone.utc


def _series(closes: list[float]) -> OhlcvSeries:
    """Hand-built series with exact closes (open=prev close) for cross tests."""
    from datetime import timedelta

    from tradingagents.marketdata.models import Bar

    bars = []
    price = closes[0]
    start = datetime(2025, 1, 1, tzinfo=UTC)
    for i, close in enumerate(closes):
        ts = start + timedelta(hours=i)
        bars.append(
            Bar(
                timestamp=ts,
                open=price, high=max(price, close) * 1.0001,
                low=min(price, close) * 0.9999, close=close, volume=10.0,
            )
        )
        price = close
    return OhlcvSeries(
        asset_id="XAUUSD", timeframe="1h", source="t",
        status=DataStatus.HISTORICAL, bars=bars,
    )


# -- baselines -----------------------------------------------------------------


def test_sma_cross_emits_buy_only_on_golden_cross() -> None:
    strat = SmaCrossStrategy(fast=2, slow=4)
    # Rising into a cross: fast crosses above slow between idx 3 and 4.
    closes = [10.0, 10.0, 10.0, 10.0, 20.0, 20.0, 20.0]
    series = _series(closes)
    signals = []
    for i in range(len(closes)):
        visible = OhlcvSeries(
            asset_id=series.asset_id, timeframe=series.timeframe,
            source=series.source, status=series.status, bars=series.bars[: i + 1],
        )
        sig = strat.generate(asset_id="X", timeframe="1h", visible=visible, now=visible.bars[-1].timestamp)
        if sig is not None:
            signals.append((i, sig.action))
    assert signals == [(4, SignalAction.BUY)]


def test_sma_cross_emits_sell_on_death_cross() -> None:
    strat = SmaCrossStrategy(fast=2, slow=4)
    closes = [20.0] * 4 + [5.0, 5.0, 5.0]
    series = _series(closes)
    signals = []
    for i in range(len(closes)):
        visible = OhlcvSeries(
            asset_id=series.asset_id, timeframe=series.timeframe,
            source=series.source, status=series.status, bars=series.bars[: i + 1],
        )
        sig = strat.generate(asset_id="X", timeframe="1h", visible=visible, now=visible.bars[-1].timestamp)
        if sig is not None:
            signals.append((i, sig.action))
    assert (4, SignalAction.SELL) in signals


def test_sma_cross_rejects_inverted_windows() -> None:
    with pytest.raises(ValueError):
        SmaCrossStrategy(fast=30, slow=10)


def test_momentum_sign_rule() -> None:
    up = make_series(40, drift_pct_per_bar=0.5, status=DataStatus.HISTORICAL)
    sig_up = MomentumStrategy(lookback=20).generate(
        asset_id="X", timeframe="1h", visible=up, now=up.bars[-1].timestamp,
    )
    assert sig_up.action is SignalAction.BUY

    down = make_series(40, drift_pct_per_bar=-0.5, status=DataStatus.HISTORICAL)
    sig_down = MomentumStrategy(lookback=20).generate(
        asset_id="X", timeframe="1h", visible=down, now=down.bars[-1].timestamp,
    )
    assert sig_down.action is SignalAction.SELL

    # Not enough history -> no decision at all.
    short = make_series(10, status=DataStatus.HISTORICAL)
    assert (
        MomentumStrategy(lookback=20).generate(
            asset_id="X", timeframe="1h", visible=short,
            now=short.bars[-1].timestamp,
        )
        is None
    )


def test_buy_and_hold_enters_exactly_once() -> None:
    strat = BuyAndHoldStrategy()
    s1 = make_series(30, status=DataStatus.HISTORICAL)
    ts = s1.bars[-1].timestamp
    first = strat.generate(asset_id="X", timeframe="1h", visible=s1, now=ts)
    assert first is not None and first.action is SignalAction.BUY
    second = strat.generate(asset_id="X", timeframe="1h", visible=s1, now=ts)
    assert second is None


def test_baseline_levels_follow_assembly_methodology() -> None:
    """ATR stop distance and 2R target mirror the research assembly constants."""
    series = make_series(
        40, zigzag_pct_per_bar=0.6, status=DataStatus.HISTORICAL,
    )
    sig = MomentumStrategy(lookback=20).generate(
        asset_id="X", timeframe="1h", visible=series, now=series.bars[-1].timestamp,
    )
    assert sig is not None and sig.stop_loss_reference is not None
    close = series.latest_close
    from tradingagents.analysis.indicators import compute_indicators

    snap = compute_indicators(series)
    atr14 = float(snap.indicators["atr_14"])
    risk_dist = STOP_ATR_MULTIPLE * atr14
    reward_dist = REWARD_RISK_RATIO * risk_dist
    if sig.action is SignalAction.BUY:
        assert sig.stop_loss_reference == pytest.approx(close - risk_dist, abs=1e-6)
        assert sig.take_profit_reference == pytest.approx(close + reward_dist, abs=1e-6)
    else:
        assert sig.stop_loss_reference == pytest.approx(close + risk_dist, abs=1e-6)
        assert sig.take_profit_reference == pytest.approx(close - reward_dist, abs=1e-6)


# -- walk-forward ---------------------------------------------------------------


def test_generate_windows_frame_math() -> None:
    cfg = WalkForwardConfig(train_bars=300, validation_bars=100, test_bars=100, step_bars=200)
    frames = generate_windows(1000, cfg)
    # size=500, step=200 -> starts at 0, 200, 400 (600+500 > 1000).
    assert len(frames) == 3
    f1, f2, f3 = frames
    assert (f1.train_start, f1.train_end, f1.validation_end, f1.test_end) == (0, 300, 400, 500)
    assert (f2.train_start, f2.test_end) == (200, 700)
    assert (f3.train_start, f3.test_end) == (400, 900)
    # Non-overlapping test phases when step >= test size.
    assert f2.validation_end >= f1.test_end


def test_generate_windows_empty_when_too_short() -> None:
    cfg = WalkForwardConfig(train_bars=300, validation_bars=100, test_bars=100)
    assert generate_windows(450, cfg) == []


def test_aggregate_median_and_profitability() -> None:
    def win(wid: int, ret: float, trades: int = 10) -> object:
        from tradingagents.backtest.walkforward import WindowResult

        return WindowResult(
            window_id=wid, strategy_id="s",
            train_period=("a", "b"), test_period=("c", "d"),
            trades=trades, total_return_pct=ret, max_drawdown_pct=abs(ret),
        )

    results = [win(1, 5.0), win(2, -1.0), win(3, 3.0)]
    agg = aggregate_window_results(results)
    assert agg.strategy_id == "s"
    assert agg.n_windows == 3
    assert agg.median_window_return_pct == pytest.approx(3.0)
    assert agg.best_window_return_pct == pytest.approx(5.0)
    assert agg.worst_window_return_pct == pytest.approx(-1.0)
    assert agg.profitable_windows == 2
    assert agg.pct_profitable == pytest.approx(200 / 3)


def test_aggregate_flags_low_trade_count_diagnostic() -> None:
    from tradingagents.backtest.walkforward import WindowResult

    def win(wid: int, ret: float, trades: int) -> WindowResult:
        return WindowResult(
            window_id=wid, strategy_id="s",
            train_period=("a", "b"), test_period=("c", "d"),
            trades=trades, total_return_pct=ret,
        )

    results = [win(1, 2.0, 3), win(2, 1.0, 30), win(3, 0.5, 25)]
    agg = aggregate_window_results(results)
    assert "trade_count" in agg.overfitting_diagnostics


def test_aggregate_handles_no_usable_windows() -> None:
    from tradingagents.backtest.walkforward import WindowResult

    skipped = WindowResult(
        window_id=1, strategy_id="s", train_period=("a", "b"),
        test_period=("c", "d"), trades=0, skipped_reason="too few bars",
    )
    agg = aggregate_window_results([skipped])
    assert agg.n_windows == 1
    assert agg.average_window_return_pct is None
    assert "no completed windows" in agg.consistency_note
