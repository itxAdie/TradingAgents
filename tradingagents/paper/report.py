"""Paper-trading account report.

Structured JSON-first report over the *actual persisted account* — every
number comes from the store, nothing is recomputed differently. Text
rendering is secondary. The Phase 4 dashboard will consume the JSON form.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from tradingagents.backtest.analytics import PerformanceStats
from tradingagents.paper.models import (
    AccountState,
    DailyPerformanceRow,
    PositionRecord,
)

PAPER_DISCLAIMER = (
    "PAPER TRADING — SIMULATED EXECUTION ONLY. Virtual account, no real "
    "money, no broker orders. Simulated performance does not imply or "
    "guarantee any future result."
)


class OpenPositionView(BaseModel):
    """Compact open-position projection for reports/dashboards."""

    position_id: str
    asset_id: str
    direction: int
    quantity: float
    entry_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    unrealized_pnl: float | None
    signal_id: str
    opened_at: datetime


class PaperAccountReport(BaseModel):
    schema_name: str = "PAPER_ACCOUNT_REPORT"
    account_id: str
    environment: str
    generated_at: datetime
    halted: bool = False
    halt_reason: str = ""
    initial_capital: float
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    total_costs_paid: float
    total_return_pct: float
    open_positions: list[OpenPositionView] = Field(default_factory=list)
    stats: PerformanceStats
    daily: list[DailyPerformanceRow] = Field(default_factory=list)
    disclaimer: str = PAPER_DISCLAIMER

    def to_json(self, path: Path) -> Path:
        """Write the JSON artifact (parents created)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=1), encoding="utf-8")
        return path

    def render_text(self) -> str:
        s = self.stats
        lines = [
            "PAPER TRADING ACCOUNT (SIMULATED)",
            "=" * 34,
            f"Account:            {self.account_id} [{self.environment}]",
            f"Status:             {'HALTED — ' + self.halt_reason if self.halted else 'active'}",
            f"Initial capital:    {self.initial_capital:,.2f}",
            f"Cash:               {self.cash:,.2f}",
            f"Equity:             {self.equity:,.2f}",
            f"Total return:       {self.total_return_pct:+.2f}%",
            f"Realized P&L:       {self.realized_pnl:+,.2f}",
            f"Unrealized P&L:     {self.unrealized_pnl:+,.2f}",
            f"Fees paid:          {self.total_costs_paid:,.2f}",
            f"Open positions:     {len(self.open_positions)}",
            f"Closed trades:      {s.n_trades}"
            + (f"  (win rate {s.win_rate_pct}%)" if s.win_rate_pct is not None else ""),
        ]
        for pos in self.open_positions:
            side = "LONG" if pos.direction == 1 else "SHORT"
            mark = f"{pos.current_price:,.2f}" if pos.current_price else "n/a"
            pnl = (
                f"{pos.unrealized_pnl:+,.2f}"
                if pos.unrealized_pnl is not None
                else "n/a"
            )
            lines.append(
                f"  - {pos.asset_id} {side} {pos.quantity:g} @ {pos.entry_price:,.2f} "
                f"(mark {mark}, uPnL {pnl}, signal {pos.signal_id})"
            )
        if s.max_drawdown_pct is not None:
            lines.append(f"Max drawdown:       {s.max_drawdown_pct:.2f}%")
        if s.profit_factor is not None:
            lines.append(f"Profit factor:      {s.profit_factor}")
        elif "profit_factor" in s.na_reasons:
            lines.append(f"Profit factor:      N/A ({s.na_reasons['profit_factor']})")
        if self.daily:
            latest = self.daily[-1]
            lines.append(
                f"Latest day {latest.date}: equity {latest.ending_equity:,.2f} "
                f"({latest.daily_return_pct:+.2f}%), trades {latest.trades_closed}"
            )
        lines.append("")
        lines.append(self.disclaimer)
        return "\n".join(lines)


def build_account_report(
    *,
    state: AccountState,
    positions: list[PositionRecord],
    equity: float,
    unrealized_pnl: float,
    stats: PerformanceStats,
    daily: list[DailyPerformanceRow],
    now: datetime,
) -> PaperAccountReport:
    """Assemble the report from persisted state + computed marks."""
    views = [
        OpenPositionView(
            position_id=pos.position_id,
            asset_id=pos.asset_id,
            direction=pos.direction,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            current_price=pos.current_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            unrealized_pnl=pos.unrealized_pnl,
            signal_id=pos.signal_id,
            opened_at=pos.entry_time,
        )
        for pos in positions
    ]
    return PaperAccountReport(
        account_id=state.account_id,
        environment=state.environment,
        generated_at=now,
        halted=state.halted,
        halt_reason=state.halt_reason,
        initial_capital=state.initial_capital,
        cash=state.cash,
        equity=equity,
        realized_pnl=state.realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_costs_paid=state.total_costs_paid,
        total_return_pct=(equity / state.initial_capital - 1) * 100,
        open_positions=views,
        stats=stats,
        daily=daily,
    )


__all__ = [
    "PAPER_DISCLAIMER",
    "OpenPositionView",
    "PaperAccountReport",
    "build_account_report",
]
