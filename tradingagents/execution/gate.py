"""HardRiskGate + CircuitBreaker: deterministic vetoes AFTER RiskEngine (P5).

Independent from the paper RiskEngine by construction: this gate re-checks
account-level facts from the *broker snapshot* and can veto an already
approved signal (spec §8). Any exception inside a check fails closed to a
veto. Limits come only from LiveRiskLimits (operator configuration); the
AI has no write path to any of them.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradingagents.brokers.base import BrokerAccountSnapshot, ConnectionStatus
from tradingagents.execution.config import LiveRiskLimits
from tradingagents.execution.store import ExecutionStore
from tradingagents.research.schemas import SignalAction


@dataclass(frozen=True)
class GateDecision:
    approved: bool
    reason_code: str = ""
    detail: str = ""

    @classmethod
    def ok(cls) -> GateDecision:
        return cls(approved=True)

    @classmethod
    def veto(cls, code: str, detail: str) -> GateDecision:
        return cls(approved=False, reason_code=code, detail=detail)


class CircuitBreaker:
    """Persisted system breaker; manual reset only."""

    def __init__(self, store: ExecutionStore) -> None:
        self._store = store

    def tripped(self) -> bool:
        return self._store.circuit_breaker_state()[0]

    def trip(self, reason: str) -> None:
        self._store.set_circuit_breaker(True, reason)

    def reset(self, *, operator: str = "") -> None:
        # operator is recorded in the store history via set_circuit_breaker caller;
        # deliberate no-auto-reset: only explicit calls land here.
        self._store.set_circuit_breaker(False, f"manual_reset:{operator}")


class HardRiskGate:
    """Ordered vetoes over the post-RiskEngine order request."""

    def __init__(
        self,
        *,
        limits: LiveRiskLimits,
        store: ExecutionStore,
        breaker: CircuitBreaker,
    ) -> None:
        self._limits = limits
        self._store = store
        self._breaker = breaker

    def evaluate(
        self,
        *,
        action: SignalAction,
        asset_id: str,
        quantity: float,
        reference_price: float,
        stop_distance_pct: float | None,
        account: BrokerAccountSnapshot | None,
        connection: ConnectionStatus,
        open_orders_count: int,
        consecutive_losses: int,
        day_start_equity: float | None,
        peak_equity: float | None,
        realized_pnl_today: float | None,
    ) -> GateDecision:
        try:
            return self._evaluate_ordered(
                action=action,
                asset_id=asset_id,
                quantity=quantity,
                reference_price=reference_price,
                stop_distance_pct=stop_distance_pct,
                account=account,
                connection=connection,
                open_orders_count=open_orders_count,
                consecutive_losses=consecutive_losses,
                day_start_equity=day_start_equity,
                peak_equity=peak_equity,
                realized_pnl_today=realized_pnl_today,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on ANY internal error
            return GateDecision.veto("gate_error", f"hard gate internal failure: {exc}")

    def _evaluate_ordered(
        self,
        *,
        action: SignalAction,
        asset_id: str,
        quantity: float,
        reference_price: float,
        stop_distance_pct: float | None,
        account: BrokerAccountSnapshot | None,
        connection: ConnectionStatus,
        open_orders_count: int,
        consecutive_losses: int,
        day_start_equity: float | None,
        peak_equity: float | None,
        realized_pnl_today: float | None,
    ) -> GateDecision:
        limits = self._limits

        # 0. global kill switches / persisted halts ---------------------------------
        halted, halt_reason = self._store.is_halted()
        if halted:
            return GateDecision.veto("trading_halted", halt_reason or "operator halt active")
        tripped, trip_reason = self._store.circuit_breaker_state()
        if tripped:
            return GateDecision.veto("circuit_breaker", trip_reason or "breaker tripped")

        # 1. infrastructure sanity ----------------------------------------------------
        if connection is not ConnectionStatus.CONNECTED:
            return GateDecision.veto("broker_not_connected", f"connection={connection.value}")
        if account is None:
            return GateDecision.veto("account_unavailable", "no broker account snapshot")
        equity = account.equity
        if equity <= 0:
            return GateDecision.veto("account_unhealthy", f"equity={equity}")

        if action is SignalAction.HOLD:
            return GateDecision.ok()  # nothing to place; downstream no-op

        notional = quantity * reference_price

        # 2. daily loss (hard block) ---------------------------------------------------
        if realized_pnl_today is None:
            return GateDecision.veto("daily_pnl_unknown", "fail-closed: today's P&L unavailable")
        daily_loss_pct = -realized_pnl_today / day_start_equity * 100 if (
            day_start_equity and day_start_equity > 0
        ) else None
        if day_start_equity is None or day_start_equity <= 0:
            return GateDecision.veto("day_anchor_unknown", "fail-closed: day-start equity unknown")
        assert daily_loss_pct is not None
        if daily_loss_pct >= limits.max_daily_loss_pct:
            return GateDecision.veto(
                "max_daily_loss",
                f"daily loss {daily_loss_pct:.2f}% >= limit {limits.max_daily_loss_pct}%",
            )

        # 3. drawdown (persisted halt handled above; here: threshold check) --------------
        if peak_equity is None or peak_equity <= 0:
            return GateDecision.veto("peak_unknown", "fail-closed: peak equity unknown")
        drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity * 100)
        if drawdown_pct >= limits.max_drawdown_pct:
            self._store.set_halt(True, f"drawdown {drawdown_pct:.2f}% reached limit")
            return GateDecision.veto(
                "max_drawdown",
                f"drawdown {drawdown_pct:.2f}% >= limit {limits.max_drawdown_pct}% — trading halted",
            )

        # 4. consecutive losses -----------------------------------------------------------
        if consecutive_losses >= limits.max_consecutive_losses:
            self._store.set_halt(
                True, f"{consecutive_losses} consecutive losses >= {limits.max_consecutive_losses}"
            )
            return GateDecision.veto("max_consecutive_losses", "trading halted pending operator")

        # 5. exposure / leverage ------------------------------------------------------------
        gross_notional = sum(abs(p.quantity) * p.avg_entry_price for p in account.positions)
        new_gross = gross_notional + notional
        leverage = new_gross / equity if equity > 0 else float("inf")
        cap = limits.max_leverage
        if account.leverage_cap is not None:
            cap = min(cap, account.leverage_cap)  # never exceed venue maximum either
        if leverage > cap:
            return GateDecision.veto(
                "max_leverage",
                f"implied leverage {leverage:.2f}x > cap {cap:.2f}x",
            )
        total_exposure_pct = new_gross / equity * 100
        if total_exposure_pct > limits.max_total_exposure_pct:
            return GateDecision.veto(
                "max_total_exposure",
                f"exposure {total_exposure_pct:.1f}% > {limits.max_total_exposure_pct}%",
            )

        # 6. position/order counts & sizes ------------------------------------------------------
        positions_on_asset = sum(1 for p in account.positions if p.asset_id == asset_id)
        if positions_on_asset == 0 and len(account.positions) >= limits.max_open_positions:
            return GateDecision.veto(
                "max_open_positions",
                f"{len(account.positions)} open >= limit {limits.max_open_positions}",
            )
        if open_orders_count >= limits.max_open_orders:
            return GateDecision.veto(
                "max_open_orders",
                f"{open_orders_count} open orders >= limit {limits.max_open_orders}",
            )
        if notional > limits.max_order_value:
            return GateDecision.veto(
                "max_order_value",
                f"order value {notional:.2f} > {limits.max_order_value:.2f}",
            )
        if notional > limits.max_position_notional:
            return GateDecision.veto(
                "max_position_notional",
                f"position value {notional:.2f} > {limits.max_position_notional:.2f}",
            )

        # 7. risk-per-trade needs a stop ---------------------------------------------------------
        if stop_distance_pct is None or stop_distance_pct <= 0:
            return GateDecision.veto("missing_stop_level", "mandatory stop distance missing")
        risk_amount = notional * stop_distance_pct / 100
        max_risk_amount = equity * limits.max_risk_per_trade_pct / 100
        if risk_amount > max_risk_amount:
            return GateDecision.veto(
                "risk_per_trade",
                f"trade risk {risk_amount:.2f} > budget {max_risk_amount:.2f} "
                f"({limits.max_risk_per_trade_pct}% of equity)",
            )

        return GateDecision.ok()
