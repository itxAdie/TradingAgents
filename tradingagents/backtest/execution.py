"""Deterministic execution simulator.

Turns :class:`~tradingagents.research.schemas.ResearchSignal` objects into
simulated fills, stop-loss/take-profit resolutions, and ledger records.
No broker, no network, no randomness — every fill is a pure function of the
bar series and the configured costs (ARCHITECTURE.md P2.4/P2.5).

Event ordering per bar (strict, tested):

1. **Open-time events**: a pending entry/exit scheduled for this bar fills
   at ``bar.open`` (adversely adjusted by configured costs). Signals never
   fill on their own decision candle.
2. **Intra-bar resolution**: stops/targets are checked against the bar's
   range. If both stop and target fall inside one bar, the **stop wins**
   (pessimistic assumption, documented).

A signal generated at decision close ``T`` schedules its fill for the open
of bar ``T + entry_delay_bars`` (default 1). Nothing fills before that bar
actually exists in the walked timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tradingagents.backtest.config import ExecutionConfig, RiskLimits, SizingPolicy
from tradingagents.backtest.ledger import TradeLedger
from tradingagents.backtest.portfolio import Portfolio, Position, size_position
from tradingagents.marketdata.models import Bar
from tradingagents.research.schemas import ResearchSignal, SignalAction


@dataclass
class _PendingFill:
    """An entry or flip intent awaiting execution at a future bar's open."""

    kind: str  # "entry" | "flip"
    bars_remaining: int  # decremented per subsequent bar; fills at 0
    signal: ResearchSignal


class ExecutionSimulator:
    """Stateful per-strategy executor over one asset's walked timeline."""

    def __init__(
        self,
        *,
        run_id: str,
        strategy_id: str,
        asset_id: str,
        timeframe: str,
        timeframe_minutes: int,
        execution_cfg: ExecutionConfig,
        sizing: SizingPolicy,
        limits: RiskLimits,
        portfolio: Portfolio,
        ledger: TradeLedger,
    ):
        self.run_id = run_id
        self.strategy_id = strategy_id
        self.asset_id = asset_id
        self.timeframe = timeframe
        self.timeframe_minutes = timeframe_minutes
        self.cfg = execution_cfg
        self.sizing = sizing
        self.limits = limits
        self.portfolio = portfolio
        self.ledger = ledger
        self.pending: _PendingFill | None = None
        self.rejected_entries = 0

    # -- cost math -------------------------------------------------------------

    def _fill(self, raw_price: float, direction: int) -> float:
        """Adverse-adjusted fill: pay slippage + half spread + commission."""
        return raw_price * (1 + direction * self.cfg.cost_bps_per_side() / 10_000)

    def _cost_amount(self, raw_price: float, quantity: float) -> float:
        return raw_price * quantity * self.cfg.cost_bps_per_side() / 10_000

    def _bars_held(self, entry_time: datetime, exit_time: datetime) -> int:
        seconds = (exit_time - entry_time).total_seconds()
        return max(1, round(seconds / 60 / self.timeframe_minutes))

    # -- scheduling ---------------------------------------------------------------

    def schedule_signal(self, signal: ResearchSignal, now: datetime) -> None:
        """Queue one decision's intent (HOLD never schedules anything)."""
        action = signal.action
        position = self.portfolio.positions.get(self.asset_id)

        if action is SignalAction.HOLD:
            return
        if (
            position is not None
            and (
                (action is SignalAction.BUY and position.direction == 1)
                or (action is SignalAction.SELL and position.direction == -1)
            )
        ):
            return  # already positioned that way; no pyramiding

        # Replace any unexecuted pending intent with the newest decision.
        self.pending = _PendingFill(
            kind="flip" if position is not None else "entry",
            bars_remaining=self.cfg.entry_delay_bars,
            signal=signal,
        )

    # -- walking ------------------------------------------------------------------

    def on_bar_open(self, bar: Bar) -> None:
        """Execute any pending fill once its delay has elapsed.

        The walker calls this for every bar after the decision bar, so the
        earliest possible fill is the bar following the decision close —
        a decision candle can never fill its own signal.
        """
        pending = self.pending
        if pending is None:
            return
        if pending.bars_remaining > 1:
            pending.bars_remaining -= 1
            return
        signal = pending.signal
        position = self.portfolio.positions.get(self.asset_id)

        # Exit leg first (flip), then the entry leg at the same open.
        if position is not None:
            direction = -position.direction
            fill = self._fill(bar.open, direction)
            gross = position.direction * (bar.open - position.entry_price) * position.quantity
            costs = self._cost_amount(bar.open, position.quantity)
            self.portfolio.close_position(
                self.asset_id, net_pnl=gross - costs, costs_paid=0.0
            )
            self._record(
                position=position, raw_exit=bar.open, exit_fill=fill,
                exit_time=bar.timestamp, gross=gross, costs=costs,
                reason="signal_exit",
            )

        action = signal.action
        direction = 1 if action is SignalAction.BUY else -1
        self._open(signal=signal, raw_price=bar.open, bar=bar, direction=direction)
        self.pending = None

    def _open(
        self, *, signal: ResearchSignal, raw_price: float, bar: Bar,
        direction: int,
    ) -> None:
        mark = {self.asset_id: raw_price}
        equity = self.portfolio.equity(mark)
        if not self.portfolio.can_open(limits_max_open=self.limits.max_open_positions):
            self.rejected_entries += 1
            return
        qty = size_position(policy=self.sizing, equity=equity, price=raw_price)
        if qty * raw_price > self.limits.max_position_notional:
            qty = self.limits.max_position_notional / raw_price
        exposure_pct = qty * raw_price / equity * 100 if equity > 0 else float("inf")
        if exposure_pct > self.limits.max_total_exposure_pct:
            self.rejected_entries += 1
            return
        fill = self._fill(raw_price, direction)
        position = Position(
            asset_id=self.asset_id,
            direction=direction,
            quantity=qty,
            entry_price=fill,
            entry_time=bar.timestamp,
            stop_loss=signal.stop_loss_reference,
            take_profit=signal.take_profit_reference,
            signal_generated_at=signal.generated_at,
            strategy_id=self.strategy_id,
            raw_entry_price=raw_price,
        )
        self.portfolio.open_position(position)

    def on_bar_close(self, bar: Bar) -> str | None:
        """Resolve stop/target against one closed bar; returns exit reason."""
        position = self.portfolio.positions.get(self.asset_id)
        if position is None:
            return None
        d = position.direction
        hit_stop = position.stop_loss is not None and (
            bar.low <= position.stop_loss if d == 1 else bar.high >= position.stop_loss
        )
        hit_tp = position.take_profit is not None and (
            bar.high >= position.take_profit if d == 1 else bar.low <= position.take_profit
        )
        if hit_stop:  # pessimistic: stop wins when both touch in one bar
            raw, reason = position.stop_loss, "stop_loss"
        elif hit_tp:
            raw, reason = position.take_profit, "take_profit"
        else:
            return None
        exit_fill = self._fill(raw, -d)
        gross = d * (raw - position.entry_price) * position.quantity
        costs = self._cost_amount(raw, position.quantity)
        self.portfolio.close_position(
            self.asset_id, net_pnl=gross - costs, costs_paid=0.0
        )
        self._record(
            position=position, raw_exit=raw, exit_fill=exit_fill,
            exit_time=bar.timestamp, gross=gross, costs=costs, reason=reason,
        )
        return reason

    def force_close_last_bar(self, last_bar: Bar) -> None:
        """End-of-data settlement at the final close, labelled honestly."""
        position = self.portfolio.positions.get(self.asset_id)
        if position is None:
            return
        d = position.direction
        exit_fill = self._fill(last_bar.close, -d)
        gross = d * (last_bar.close - position.entry_price) * position.quantity
        costs = self._cost_amount(last_bar.close, position.quantity)
        self.portfolio.close_position(
            self.asset_id, net_pnl=gross - costs, costs_paid=0.0
        )
        self._record(
            position=position, raw_exit=last_bar.close, exit_fill=exit_fill,
            exit_time=last_bar.timestamp, gross=gross, costs=costs,
            reason="end_of_data",
        )
        self.pending = None

    # -- ledger ---------------------------------------------------------------

    def _record(
        self,
        *,
        position: Position,
        raw_exit: float,
        exit_fill: float,
        exit_time: datetime,
        gross: float,
        costs: float,
        reason: str,
    ) -> None:
        self.ledger.append(
            strategy_id=position.strategy_id,
            asset_id=position.asset_id,
            timeframe=self.timeframe,
            direction=position.direction,
            signal_generated_at=position.signal_generated_at or exit_time,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            raw_entry_price=position.raw_entry_price or position.entry_price,
            exit_time=exit_time,
            exit_price=exit_fill,
            raw_exit_price=raw_exit,
            quantity=position.quantity,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            gross_pnl=gross,
            transaction_costs=costs,
            bars_held=self._bars_held(position.entry_time, exit_time),
            exit_reason=reason,
        )


__all__ = ["ExecutionSimulator"]
