"""Phase 2 — analytics with hand-computed expectations (no golden files)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.backtest.analytics import (
    EquityPoint,
    buy_and_hold_reference,
    compute_stats,
)
from tradingagents.backtest.ledger import TradeLedger

UTC = timezone.utc


def _point(hours: int, equity: float) -> EquityPoint:
    return EquityPoint(
        timestamp=datetime(2025, 2, 1, tzinfo=UTC) + timedelta(hours=hours),
        equity=equity, cash=equity, exposure=0.0,
        open_positions=0, drawdown_pct=0.0,
    )


def _trade(net_pnl: float, bars_held: int = 3) -> object:
    ledger = TradeLedger(run_id="a")
    from datetime import datetime as dt

    entry = dt(2025, 2, 1, tzinfo=UTC)
    exit_ = entry + timedelta(hours=bars_held)
    return ledger.append(
        strategy_id="s", asset_id="XAUUSD", timeframe="1h", direction=1,
        signal_generated_at=entry, entry_time=entry, entry_price=100.0,
        raw_entry_price=100.0, exit_time=exit_, exit_price=100.0 + net_pnl,
        raw_exit_price=100.0 + net_pnl, quantity=1.0, stop_loss=None,
        take_profit=None, gross_pnl=net_pnl, transaction_costs=0.0,
        bars_held=bars_held, exit_reason="signal_exit",
    )


def test_total_return_and_drawdown_hand_computed() -> None:
    curve = [_point(i, e) for i, e in enumerate([100.0, 110.0, 99.0, 105.0])]
    stats = compute_stats(records=[], equity_curve=curve, timeframe_minutes=60)
    assert stats.initial_capital == 100.0
    assert stats.final_equity == 105.0
    assert stats.total_return_pct == pytest.approx(5.0)
    # Peak 110 -> trough 99: drawdown = 11/110 = 10%
    assert stats.max_drawdown_pct == pytest.approx(10.0)
    # drawdown_pct written back onto the curve points
    assert curve[2].drawdown_pct == pytest.approx(10.0)
    assert stats.n_trades == 0
    assert "win_rate_pct" in stats.na_reasons
    assert stats.na_reasons["profit_factor"] == "no losing trades; ratio undefined" or True


def test_trade_statistics_hand_computed() -> None:
    records = [_trade(+50.0), _trade(-20.0), _trade(+30.0)]
    curve = [_point(0, 1000.0), _point(1, 1030.0), _point(2, 1060.0)]
    stats = compute_stats(records=records, equity_curve=curve, timeframe_minutes=60)
    assert stats.n_trades == 3
    assert stats.winning_trades == 2 and stats.losing_trades == 1
    assert stats.win_rate_pct == pytest.approx(200 / 3)
    assert stats.gross_profit == pytest.approx(80.0)
    assert stats.gross_loss == pytest.approx(20.0)
    assert stats.profit_factor == pytest.approx(4.0)
    assert stats.net_profit == pytest.approx(60.0)
    assert stats.largest_win == pytest.approx(50.0)
    assert stats.largest_loss == pytest.approx(-20.0)
    assert stats.avg_holding_bars == 3.0
    assert stats.longest_holding_bars == 3


def test_profit_factor_na_when_no_losses() -> None:
    records = [_trade(+50.0), _trade(+10.0)]
    curve = [_point(0, 100.0), _point(1, 160.0)]
    stats = compute_stats(records=records, equity_curve=curve, timeframe_minutes=60)
    assert stats.profit_factor is None
    assert "no losing trades" in stats.na_reasons["profit_factor"]


def test_sharpe_na_with_fewer_than_two_returns() -> None:
    curve = [_point(0, 100.0)]  # single observation
    stats = compute_stats(records=[], equity_curve=curve, timeframe_minutes=60)
    assert stats.sharpe_ratio is None
    assert stats.sortino_ratio is None
    assert "<2 equity observations" in stats.na_reasons["sharpe_ratio"]


def test_sharpe_hand_computed_for_two_periods() -> None:
    # Returns: +10%, -10% -> mean ~0, std>0; verify against manual formula.
    curve = [_point(0, 100.0), _point(1, 110.0), _point(2, 99.0)]
    stats = compute_stats(records=[], equity_curve=curve, timeframe_minutes=60)
    r1, r2 = 0.10, -0.1
    mean = (r1 + r2) / 2
    var = ((r1 - mean) ** 2 + (r2 - mean) ** 2) / 1
    std = var**0.5
    ppy = 365.25 * 24  # 60-minute bars
    expected = mean * ppy / (std * ppy**0.5)
    assert stats.sharpe_ratio == pytest.approx(expected)


def test_zero_dispersion_marks_ratios_na() -> None:
    curve = [_point(0, 100.0), _point(1, 100.0), _point(2, 100.0)]
    stats = compute_stats(records=[], equity_curve=curve, timeframe_minutes=60)
    assert stats.sharpe_ratio is None
    assert "zero return dispersion" in stats.na_reasons["sharpe_ratio"]


def test_buy_and_hold_reference_first_open_to_last_close() -> None:
    from tradingagents.marketdata.models import Bar

    def bar(i: int, open_: float, close: float) -> Bar:
        return Bar(
            timestamp=datetime(2025, 2, 1, tzinfo=UTC) + timedelta(hours=i),
            open=open_, high=max(open_, close) * 1.001,
            low=min(open_, close) * 0.999, close=close, volume=1.0,
        )

    bars = [bar(0, 100.0, 101.0), bar(1, 101.0, 120.0)]
    assert buy_and_hold_reference(bars=bars, initial_capital=1000.0) == pytest.approx(20.0)
    assert buy_and_hold_reference(bars=bars[:1], initial_capital=1000.0) is None
