"""Deterministic risk engine.

The AI never controls risk. Every check is a pure function of account state
and configuration, evaluated in a fixed documented order; the first failing
check wins and its ``reason_code`` is logged with the rejection. A valid AI
signal can always be vetoed here — never the reverse (ARCHITECTURE.md P3.1,
PROJECT_RULES §6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tradingagents.paper.config import PaperRiskLimits
from tradingagents.research.schemas import SignalAction


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of one risk evaluation."""

    approved: bool
    reason_code: str = ""
    detail: str = ""


def _approve() -> RiskDecision:
    return RiskDecision(approved=True)


def _veto(reason_code: str, detail: str) -> RiskDecision:
    return RiskDecision(approved=False, reason_code=reason_code, detail=detail)


class RiskEngine:
    """Ordered deterministic vetoes; first failure wins."""

    def __init__(
        self,
        *,
        limits: PaperRiskLimits,
        kill_switch_enabled: bool,
        is_halted: Callable[[], tuple[bool, str]],
    ):
        self._limits = limits
        self._kill_switch_enabled = kill_switch_enabled
        self._is_halted = is_halted

    def evaluate(
        self,
        *,
        action: SignalAction,
        entry_price: float,
        stop_loss: float | None,
        quantity: float,
        mark_price: float,
        equity: float,
        day_start_equity: float | None,
        peak_equity: float | None,
        open_positions: int,
        gross_exposure: float,
    ) -> RiskDecision:
        limits = self._limits

        # 1. kill switch (config-level)
        if not self._kill_switch_enabled:
            return _veto("trading_disabled", "paper trading is disabled by configuration")

        # 2. emergency halt (persisted flag)
        halted, halt_reason = self._is_halted()
        if halted:
            return _veto("emergency_halt", halt_reason or "paper trading is halted")

        if action is SignalAction.HOLD:
            return _approve()  # nothing to size; engine never schedules HOLD

        # 3. max daily loss (equity including unrealized vs day start)
        if day_start_equity is not None and day_start_equity > 0:
            daily_return = equity / day_start_equity - 1
            if daily_return <= -limits.max_daily_loss_pct:
                return _veto(
                    "daily_loss_limit",
                    f"daily return {daily_return:.2%} breached "
                    f"-{limits.max_daily_loss_pct:.2%} limit",
                )

        # 4. max drawdown from peak equity
        if peak_equity is not None and peak_equity > 0:
            drawdown = 1 - equity / peak_equity
            if drawdown >= limits.max_drawdown_pct:
                return _veto(
                    "max_drawdown",
                    f"drawdown {drawdown:.2%} reached {limits.max_drawdown_pct:.2%} limit",
                )

        # 5. simultaneous positions
        if open_positions >= limits.max_open_positions:
            return _veto(
                "max_positions",
                f"{open_positions} open positions at limit {limits.max_open_positions}",
            )

        # 6. every directional trade must carry a stop (AI cannot remove risk)
        if action in (SignalAction.BUY, SignalAction.SELL) and stop_loss is None:
            return _veto("missing_stop_level", "directional signal without stop loss")

        # 7. risk per trade: |entry - stop| * qty must fit the per-trade budget
        if equity <= 0:
            return _veto("insufficient_equity", f"equity {equity} not positive")
        risk_amount = quantity * abs(entry_price - stop_loss) if stop_loss else 0.0
        if risk_amount / equity > limits.max_risk_per_trade_pct:
            return _veto(
                "risk_per_trade",
                f"trade risk {risk_amount / equity:.2%} exceeds "
                f"{limits.max_risk_per_trade_pct:.2%} of equity",
            )

        # 8. total exposure cap (projected post-trade)
        projected = gross_exposure + quantity * mark_price
        exposure_pct = projected / equity * 100
        if exposure_pct > limits.max_total_exposure_pct:
            return _veto(
                "max_exposure",
                f"projected exposure {exposure_pct:.1f}% exceeds "
                f"{limits.max_total_exposure_pct:.1f}% cap",
            )

        # 9. single-position notional cap
        if quantity * entry_price > limits.max_position_notional:
            return _veto(
                "position_notional_cap",
                f"notional {quantity * entry_price:.0f} exceeds "
                f"{limits.max_position_notional:.0f}",
            )

        return _approve()


__all__ = ["RiskDecision", "RiskEngine"]
