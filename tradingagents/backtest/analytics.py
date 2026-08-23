"""Performance analytics.

Pure functions over ledger + equity-curve data. Statistical integrity rules:

- A metric that cannot be computed meaningfully (zero standard deviation,
  no losing trades for profit factor, too few observations) is reported as
  ``None`` with an explicit reason — rendered ``N/A (reason)``. We never
  emit a number just to fill a table.
- Annualization uses the *actual* bar cadence of the equity curve
  (``periods_per_year = 365*24*60 / timeframe_minutes``), not an assumed
  trading calendar.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from tradingagents.backtest.ledger import TradeRecord


class EquityPoint(BaseModel):
    """One mark-to-market observation of the portfolio (dashboard-ready)."""

    timestamp: datetime
    equity: float
    cash: float
    exposure: float
    open_positions: int
    drawdown_pct: float  # 0 at highs; positive in drawdown


class PerformanceStats(BaseModel):
    """Backtest performance report values; ``None`` = N/A (see reasons)."""

    initial_capital: float
    final_equity: float
    total_return_pct: float | None = None
    annualized_return_pct: float | None = None
    n_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate_pct: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None
    average_trade_pnl: float | None = None
    expectancy_per_trade: float | None = None
    gross_profit: float | None = None
    gross_loss: float | None = None
    net_profit: float | None = None
    profit_factor: float | None = None
    max_drawdown_pct: float | None = None
    average_drawdown_pct: float | None = None
    volatility_annualized_pct: float | None = None
    downside_deviation_annualized_pct: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    avg_holding_bars: float | None = None
    longest_holding_bars: int | None = None
    trades_per_year: float | None = None
    benchmark_buy_hold_return_pct: float | None = None
    na_reasons: dict[str, str] = Field(default_factory=dict)


def _na(stats: PerformanceStats, key: str, reason: str) -> None:
    stats.na_reasons[key] = reason


def compute_stats(
    *,
    records: list[TradeRecord],
    equity_curve: list[EquityPoint],
    timeframe_minutes: int,
) -> PerformanceStats:
    if not equity_curve:
        raise ValueError("equity curve is required to compute performance")
    first, last = equity_curve[0], equity_curve[-1]
    years = max(
        (last.timestamp - first.timestamp).total_seconds() / (365.25 * 24 * 3600),
        1e-9,
    )
    periods_per_year = 365.25 * 24 * 60 / timeframe_minutes

    stats = PerformanceStats(
        initial_capital=first.equity,
        final_equity=last.equity,
    )

    # -- returns -----------------------------------------------------------------
    if first.equity > 0:
        total_ret = (last.equity / first.equity - 1) * 100
        stats.total_return_pct = total_ret
        try:
            growth = math.log(last.equity / first.equity) / years
            stats.annualized_return_pct = (math.exp(growth) - 1) * 100
        except (OverflowError, ValueError):
            # Window far shorter than a year with extreme returns: an
            # annualized number would be meaningless (e.g. 10% in 1 bar).
            _na(stats, "annualized_return_pct", "window too short to annualize meaningfully")
    else:
        _na(stats, "total_return_pct", "initial capital must be positive")

    # -- trade statistics ----------------------------------------------------------
    pnls = [r.net_pnl for r in records]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    stats.n_trades = len(records)
    stats.winning_trades = len(wins)
    stats.losing_trades = len(losses)
    if records:
        stats.win_rate_pct = len(wins) / len(records) * 100
        stats.average_win = sum(wins) / len(wins) if wins else 0.0
        stats.average_loss = sum(losses) / len(losses) if losses else 0.0
        stats.largest_win = max(pnls)
        stats.largest_loss = min(pnls)
        stats.average_trade_pnl = sum(pnls) / len(pnls)
        win_rate = len(wins) / len(records)
        avg_win = stats.average_win or 0.0
        avg_loss = abs(stats.average_loss) if losses else 0.0
        stats.expectancy_per_trade = win_rate * avg_win - (1 - win_rate) * avg_loss
        stats.gross_profit = sum(wins)
        stats.gross_loss = abs(sum(losses))
        stats.net_profit = sum(pnls)
        if not losses or stats.gross_loss == 0:
            _na(stats, "profit_factor", "no losing trades; ratio undefined")
        else:
            stats.profit_factor = stats.gross_profit / stats.gross_loss
        holds = [r.bars_held for r in records]
        stats.avg_holding_bars = sum(holds) / len(holds)
        stats.longest_holding_bars = max(holds)
        stats.trades_per_year = len(records) / years
    else:
        for key in (
            "win_rate_pct", "expectancy_per_trade", "profit_factor",
            "avg_holding_bars", "trades_per_year",
        ):
            _na(stats, key, "no closed trades")

    # -- drawdowns from the equity curve --------------------------------------------
    peak = equity_curve[0].equity
    dds: list[float] = []
    for point in equity_curve:
        peak = max(peak, point.equity)
        dd = (peak - point.equity) / peak * 100 if peak > 0 else 0.0
        point.drawdown_pct = dd
        dds.append(dd)
    stats.max_drawdown_pct = max(dds) if dds else 0.0
    nonzero = [d for d in dds if d > 1e-12]
    stats.average_drawdown_pct = (
        sum(nonzero) / len(nonzero) if nonzero else 0.0
    )
    if stats.max_drawdown_pct and stats.max_drawdown_pct > 1e-12:
        if stats.annualized_return_pct is not None:
            stats.calmar_ratio = stats.annualized_return_pct / stats.max_drawdown_pct
    else:
        _na(stats, "calmar_ratio", "zero max drawdown; ratio undefined")

    # -- risk-adjusted metrics ---------------------------------------------------------
    returns = [
        (b.equity / a.equity - 1)
        for a, b in zip(equity_curve, equity_curve[1:], strict=False)
        if a.equity > 0
    ]
    if len(returns) < 2:
        _na(stats, "volatility_annualized_pct", "<2 equity observations")
        _na(stats, "sharpe_ratio", "<2 equity observations")
        _na(stats, "sortino_ratio", "<2 equity observations")
        return stats
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std < 1e-15:
        _na(stats, "volatility_annualized_pct", "zero return dispersion")
        _na(stats, "sharpe_ratio", "zero return dispersion")
        _na(stats, "sortino_ratio", "zero return dispersion")
        return stats
    ann = math.sqrt(periods_per_year)
    stats.volatility_annualized_pct = std * ann * 100
    downside = [r for r in returns if r < 0]
    if len(downside) >= 2:
        d_var = sum(r**2 for r in downside) / (len(downside) - 1)
        d_std = math.sqrt(d_var)
        if d_std > 1e-15:
            stats.downside_deviation_annualized_pct = d_std * ann * 100
            stats.sortino_ratio = mean_r * periods_per_year / (d_std * ann)
        else:
            _na(stats, "sortino_ratio", "no downside dispersion")
    elif not downside:
        stats.downside_deviation_annualized_pct = 0.0
        stats.sortino_ratio = None
        _na(stats, "sortino_ratio", "no losing periods to measure")
    stats.sharpe_ratio = mean_r * periods_per_year / (std * ann)
    return stats


def buy_and_hold_reference(
    *, bars: list[Any], initial_capital: float
) -> float | None:
    """Buy&hold total-return % over the same window (first-open → last-close)."""
    if len(bars) < 2:
        return None
    entry = bars[0].open
    exit_ = bars[-1].close
    return (exit_ / entry - 1) * 100


__all__ = ["EquityPoint", "PerformanceStats", "buy_and_hold_reference", "compute_stats"]
