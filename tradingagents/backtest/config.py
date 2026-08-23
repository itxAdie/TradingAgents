"""Deterministic execution-simulation configuration.

Pure configuration objects; every number that influences simulated fills
lives here (never hardcoded in the engine) so backtests are reproducible and
assumptions are disclosed in reports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SizingPolicy(BaseModel):
    """Deterministic position sizing — the LLM never sizes positions."""

    mode: Literal["fixed_notional", "fixed_quantity", "pct_equity"] = "pct_equity"
    value: float = Field(default=0.95, gt=0)
    # fixed_notional / fixed_quantity: currency units / underlying units.
    # pct_equity: fraction of current equity committed per entry.


class RiskLimits(BaseModel):
    """Hard deterministic caps evaluated before every entry."""

    max_position_notional: float = Field(default=50_000.0, gt=0)
    max_open_positions: int = Field(default=1, ge=1)
    max_total_exposure_pct: float = Field(default=100.0, gt=0, le=100)


class ExecutionConfig(BaseModel):
    """Costs, delays, and fill assumptions of the simulated venue.

    Cost model (all bps of traded notional, applied per side):

    - ``slippage_bps``: adverse drift applied to every fill price.
    - ``spread_bps``: full quoted spread; each side pays half.
    - ``commission_per_side_bps``: broker/venue fee per fill.

    Entry model: a signal generated at decision close ``T`` fills at the
    **open of bar ``T + entry_delay_bars``** (default 1 — never the decision
    candle's own close/open). Stop/target resolution is pessimistic: if a
    single bar touches both, the stop wins (P2.4).
    """

    slippage_bps: float = Field(default=1.0, ge=0)
    spread_bps: float = Field(default=2.0, ge=0)
    commission_per_side_bps: float = Field(default=0.5, ge=0)
    entry_delay_bars: int = Field(default=1, ge=1)

    def cost_bps_per_side(self) -> float:
        """All-in adverse cost in bps applied to one fill."""
        return self.slippage_bps + self.spread_bps / 2 + self.commission_per_side_bps


def adverse_fill(price: float, direction: int, cfg: ExecutionConfig) -> float:
    """Fill price after paying the all-in cost against the trade direction."""
    factor = 1 + direction * cfg.cost_bps_per_side() / 10_000
    return price * factor


__all__ = ["ExecutionConfig", "RiskLimits", "SizingPolicy", "adverse_fill"]
