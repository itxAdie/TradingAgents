"""Deterministic portfolio accounting.

Margin-account style simulation: ``cash`` holds *settled* capital (it only
changes when a position closes and realizes P&L); open positions contribute
mark-to-market unrealized P&L on top. This models XAUUSD/BTCUSD CFD/futures-
like exposure without financing costs or margin calls — a documented
simplification (see ARCHITECTURE.md P2.5); it never affects signal logic.

All mutations are driven by explicit events from the execution simulator:
no internal clock, no hidden transitions, fully deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tradingagents.backtest.config import SizingPolicy


@dataclass
class Position:
    """An open simulated position (direction: +1 long / -1 short)."""

    asset_id: str
    direction: int  # +1 / -1; validated below
    quantity: float
    entry_price: float  # actual adverse-adjusted fill price
    entry_time: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    signal_generated_at: datetime | None = None
    strategy_id: str = ""
    raw_entry_price: float = 0.0  # market price touched pre-costs

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError("direction must be +1 (long) or -1 (short)")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")

    @property
    def notional(self) -> float:
        return self.quantity * self.entry_price


@dataclass
class Portfolio:
    """Cash + positions book; currency units are the quote currency."""

    initial_capital: float
    realized_pnl: float = 0.0
    total_costs_paid: float = 0.0
    closed_trades: int = 0
    positions: dict[str, Position] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        self.cash: float = self.initial_capital  # settled capital

    # -- queries ---------------------------------------------------------------

    def unrealized_pnl(self, mark_prices: dict[str, float]) -> float:
        return sum(
            pos.direction * (mark_prices[asset_id] - pos.entry_price) * pos.quantity
            for asset_id, pos in self.positions.items()
            if asset_id in mark_prices
        )

    def equity(self, mark_prices: dict[str, float]) -> float:
        return self.cash + self.unrealized_pnl(mark_prices)

    def gross_exposure(self, mark_prices: dict[str, float]) -> float:
        """Absolute market value of open positions at mark prices."""
        return sum(
            pos.quantity * mark_prices.get(asset_id, pos.entry_price)
            for asset_id, pos in self.positions.items()
        )

    # -- mutations ---------------------------------------------------------------

    def can_open(self, *, limits_max_open: int) -> bool:
        """Risk pre-check: position slots available and settled cash positive."""
        return len(self.positions) < limits_max_open and self.cash > 0

    def open_position(self, position: Position) -> None:
        if position.asset_id in self.positions:
            raise ValueError(
                f"position already open for {position.asset_id}; "
                "pyramiding is not supported"
            )
        self.positions[position.asset_id] = position

    def close_position(
        self,
        asset_id: str,
        *,
        net_pnl: float,
        costs_paid: float,
    ) -> Position:
        """Settle a closed position: fold net P&L into cash."""
        pos = self.positions.pop(asset_id)
        self.cash += net_pnl
        self.realized_pnl += net_pnl
        self.total_costs_paid += costs_paid
        self.closed_trades += 1
        return pos


def size_position(
    *, policy: SizingPolicy, equity: float, price: float
) -> float:
    """Quantity implied by the sizing policy at ``price`` (deterministic)."""
    if policy.mode == "fixed_quantity":
        if policy.value <= 0:
            raise ValueError("fixed_quantity must be positive")
        return policy.value
    notional = policy.value if policy.mode == "fixed_notional" else equity * policy.value
    if price <= 0:
        raise ValueError("price must be positive to size a position")
    return notional / price


__all__ = ["Portfolio", "Position", "size_position"]
