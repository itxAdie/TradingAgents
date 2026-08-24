"""Live performance math: reuses backtest analytics, calendar windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.paper.models import EquitySnapshot
from tradingagents.paper.performance import (
    build_daily_row,
    live_performance_stats,
    peak_equity,
    period_pnl,
)

NOW = datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)


def snap(minutes_after_start: int, equity: float, **extra) -> EquitySnapshot:
    return EquitySnapshot(
        timestamp=NOW + timedelta(minutes=minutes_after_start),
        equity=equity,
        cash=equity,
        exposure=0.0,
        open_positions=0,
        drawdown_pct=0.0,
        **extra,
    )


class TestStatsDelegation:
    def test_curve_only_stats_have_na_reasons_not_crashes(self) -> None:
        curve = [snap(0, 10_000.0), snap(60, 10_010.0), snap(120, 9_990.0)]
        stats = live_performance_stats(records=[], equity_curve=curve)
        assert stats.n_trades == 0
        # statistically meaningless values stay N/A with a reason
        assert "profit_factor" in stats.na_reasons

    def test_peak_equity(self) -> None:
        curve = [snap(0, 100.0), snap(1, 250.0), snap(2, 200.0)]
        assert peak_equity(curve) == 250.0
        assert peak_equity([]) is None


class TestPeriodPnl:
    def test_daily_weekly_monthly_windows_utc(self) -> None:
        assert NOW.weekday() == 0, "test assumes NOW is a Monday"
        month_ago = datetime(2026, 7, 15, tzinfo=timezone.utc)
        in_month_before_week = datetime(2026, 8, 10, tzinfo=timezone.utc)
        week_start = NOW.replace(hour=0, minute=0)
        curve = [
            EquitySnapshot(
                timestamp=ts,
                equity=eq,
                cash=eq,
                exposure=0.0,
                open_positions=0,
                drawdown_pct=0.0,
            )
            for ts, eq in (
                (month_ago, 9_000.0),  # before the monthly window
                (in_month_before_week, 9_900.0),  # inside month, before week
                (week_start, 10_000.0),  # week + day start
                (NOW, 10_150.0),  # latest
            )
        ]
        result = period_pnl(curve=curve, records=[], now=NOW)
        assert result["daily"]["total_pnl"] == pytest.approx(150.0)
        assert result["weekly"]["total_pnl"] == pytest.approx(150.0)
        # weekly window starts at Monday 00:00 -> start equity 10_000
        assert result["weekly"]["start_equity"] == 10_000.0
        assert result["monthly"]["total_pnl"] == pytest.approx(250.0)
        assert result["monthly"]["start_equity"] == 9_900.0


class TestDailyRow:
    def test_row_folds_day(self) -> None:
        from tradingagents.backtest.ledger import TradeRecord

        day_curve = [snap(0, 10_000.0), snap(60, 10_100.0)]
        trade = TradeRecord(
            trade_id="T1",
            run_id="r",
            strategy_id="s",
            asset_id="XAUUSD",
            timeframe="1h",
            direction=1,
            signal_generated_at=NOW,
            entry_timestamp=NOW,
            entry_price=2000.0,
            raw_entry_price=2000.0,
            exit_timestamp=NOW + timedelta(hours=2),
            exit_price=2050.0,
            raw_exit_price=2050.0,
            quantity=1.0,
            gross_pnl=50.0,
            transaction_costs=0.25,
            net_pnl=49.75,
            return_pct=2.5,
            holding_period="PT2H",
            bars_held=2,
            exit_reason="take_profit",
        )
        row = build_daily_row(
            date_str="2026-08-24",
            day_curve=day_curve,
            day_trades=[trade],
            previous_end_equity=None,
        )
        assert row is not None
        assert row.starting_equity == 10_000.0
        assert row.ending_equity == 10_100.0
        assert row.realized_pnl == 49.75
        assert row.trades_closed == 1
        assert row.winning_trades == 1
        assert row.losing_trades == 0
        assert row.fees == 0.25

    def test_empty_day_returns_none(self) -> None:
        assert build_daily_row(
            date_str="2026-08-24", day_curve=[], day_trades=[], previous_end_equity=None
        ) is None
