"""Live performance calculations.

Thin wrapper over the Phase 2 analytics engine — the same
:func:`tradingagents.backtest.analytics.compute_stats` used by backtests,
plus calendar-window P&Ls and daily rollups. Statistically meaningless
values keep the Phase 2 ``N/A``-with-reason policy (never fabricated).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradingagents.backtest.analytics import PerformanceStats, compute_stats
from tradingagents.backtest.ledger import TradeRecord
from tradingagents.paper.models import DailyPerformanceRow, EquitySnapshot

WINDOW_KEYS = ("daily", "weekly", "monthly")


def live_performance_stats(
    *,
    records: list[TradeRecord],
    equity_curve: list[EquitySnapshot],
    timeframe_minutes: int = 60,
) -> PerformanceStats:
    """Same math as backtests; curve must include the capital anchor point."""
    return compute_stats(
        records=records,
        equity_curve=equity_curve,  # type: ignore[arg-type]
        timeframe_minutes=timeframe_minutes,
    )


def peak_equity(curve: list[EquitySnapshot]) -> float | None:
    if not curve:
        return None
    return max(point.equity for point in curve)


def equity_at_or_after(
    curve: list[EquitySnapshot], boundary: datetime
) -> EquitySnapshot | None:
    for point in curve:
        if point.timestamp >= boundary:
            return point
    return None


def _window_boundary(now: datetime, key: str) -> datetime:
    day_start = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if key == "daily":
        return day_start
    if key == "weekly":
        monday = day_start - timedelta(days=day_start.weekday())
        return monday
    if key == "monthly":
        return day_start.replace(day=1)
    raise ValueError(f"unknown window {key!r}")


def period_pnl(
    *,
    curve: list[EquitySnapshot],
    records: list[TradeRecord],
    now: datetime,
) -> dict[str, dict[str, float]]:
    """Calendar-window P&L in UTC.

    ``total`` is mark-to-market equity change over the window;
    ``realized`` sums closed trades whose exit fell inside it.
    """
    result: dict[str, dict[str, float]] = {}
    for key in WINDOW_KEYS:
        boundary = _window_boundary(now, key)
        start_point = equity_at_or_after(curve, boundary)
        end_equity = curve[-1].equity if curve else 0.0
        start_equity = start_point.equity if start_point else (curve[0].equity if curve else 0.0)
        realized = sum(
            record.net_pnl
            for record in records
            if record.exit_timestamp >= boundary
        )
        result[key] = {
            "start_equity": start_equity,
            "end_equity": end_equity,
            "total_pnl": end_equity - start_equity,
            "realized_pnl": realized,
        }
    return result


def build_daily_row(
    *,
    date_str: str,
    day_curve: list[EquitySnapshot],
    day_trades: list[TradeRecord],
    previous_end_equity: float | None,
) -> DailyPerformanceRow | None:
    """Fold one UTC day's snapshots + closed trades into a rollup row."""
    if not day_curve:
        return None
    starting = (
        previous_end_equity
        if previous_end_equity is not None
        else day_curve[0].equity
    )
    ending = day_curve[-1].equity
    realized = sum(record.net_pnl for record in day_trades)
    fees = sum(record.transaction_costs for record in day_trades)
    wins = sum(1 for record in day_trades if record.net_pnl > 0)
    losses = sum(1 for record in day_trades if record.net_pnl <= 0)
    daily_return = (ending / starting - 1) * 100 if starting > 0 else 0.0
    return DailyPerformanceRow(
        date=date_str,
        starting_equity=starting,
        ending_equity=ending,
        daily_return_pct=daily_return,
        realized_pnl=realized,
        unrealized_pnl=day_curve[-1].unrealized_pnl,
        trades_closed=len(day_trades),
        winning_trades=wins,
        losing_trades=losses,
        fees=fees,
        drawdown_pct=day_curve[-1].drawdown_pct,
    )


__all__ = [
    "WINDOW_KEYS",
    "build_daily_row",
    "live_performance_stats",
    "peak_equity",
    "period_pnl",
]
