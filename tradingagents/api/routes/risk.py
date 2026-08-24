"""Risk dashboard endpoints: configured limits vs live utilization + events.

Utilization math mirrors paper/risk.py formulas exactly; nothing is
invented client-side (spec §19/§20/§45).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from tradingagents.api.context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["risk"])


@router.get("/risk")
def risk_status(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
):
    from datetime import datetime, timezone

    from tradingagents.api.schemas import RiskLimitValue, RiskStatusResponse

    engine = ctx.engine(environment, account)
    summary = engine.account_summary()
    limits = ctx.server_config().risk if (
        environment == ctx.settings.environment and account == ctx.settings.account_id
    ) else type(ctx.server_config().risk)()

    state = summary["state"]
    equity = float(summary["equity"])
    curve = ctx.store(environment, account).load_equity_curve()

    day_start = None
    now = datetime.now(timezone.utc)
    boundary = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for point in curve:
        if point.timestamp >= boundary:
            day_start = point.equity
            break

    peak = max((p.equity for p in curve), default=None)
    if peak is None or peak <= 0:
        peak = max(equity, state.initial_capital)

    daily_loss_pct = 0.0
    if day_start:
        daily_loss_pct = max(0.0, -(equity / day_start - 1)) * 100
    drawdown_pct = max(0.0, (1 - equity / peak)) * 100 if peak > 0 else 0.0

    gross_exposure = sum(
        p.quantity * (p.current_price or p.entry_price)
        for p in summary["positions"]
    )
    exposure_pct = gross_exposure / equity * 100 if equity > 0 else 0.0
    open_positions = len(summary["positions"])

    def pct_item(key, label, limit_value, current):
        util = current / limit_value * 100 if limit_value > 0 else 0.0
        return RiskLimitValue(
            key=key,
            label=label,
            limit_value=limit_value,
            current_value=round(current, 6),
            utilization_pct=round(util, 2),
            unit="pct",
        )

    # risk-per-trade utilization is only meaningful mid-evaluation; report 0
    # rather than inventing a hypothetical trade (spec §19: backend values only).
    limit_rows = [
        pct_item("max_daily_loss", "Max daily loss", limits.max_daily_loss_pct * 100, daily_loss_pct),
        pct_item("max_drawdown", "Max drawdown", limits.max_drawdown_pct * 100, drawdown_pct),
        pct_item("max_total_exposure", "Total exposure", limits.max_total_exposure_pct, exposure_pct),
        RiskLimitValue(
            key="max_risk_per_trade",
            label="Risk per trade budget",
            limit_value=limits.max_risk_per_trade_pct * 100,
            current_value=0.0,
            utilization_pct=0.0,
            unit="pct",
        ),
    ]
    limit_rows.append(
        RiskLimitValue(
            key="max_open_positions",
            label="Open positions",
            limit_value=float(limits.max_open_positions),
            current_value=float(open_positions),
            utilization_pct=round(open_positions / limits.max_open_positions * 100, 2),
            unit="count",
        )
    )
    limit_rows.append(
        RiskLimitValue(
            key="max_position_notional",
            label="Single position notional cap",
            limit_value=limits.max_position_notional,
            current_value=max(
                (p.quantity * (p.entry_price) for p in summary["positions"]),
                default=0.0,
            ),
            utilization_pct=round(
                max(
                    (
                        p.quantity * p.entry_price / limits.max_position_notional * 100
                        for p in summary["positions"]
                    ),
                    default=0.0,
                ),
                2,
            ),
            unit="currency",
        )
    )

    return RiskStatusResponse(
        environment=state.environment,
        account_id=state.account_id,
        halted=state.halted,
        halt_reason=state.halt_reason,
        equity=equity,
        day_start_equity=day_start,
        peak_equity=peak,
        gross_exposure=gross_exposure,
        open_positions=open_positions,
        limits=limit_rows,
    )


@router.get("/risk/events")
def risk_events(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Persisted risk evidence: rejections, expiries, stop/target exits, halt."""
    from tradingagents.api.schemas import RiskEventItem, RiskEventsPage
    from tradingagents.paper.models import OrderState, SignalState

    store = ctx.store(environment, account)
    events: list[RiskEventItem] = []

    for t in store.list_signal_transitions():
        if t.to_state == SignalState.REJECTED.value:
            events.append(
                RiskEventItem(
                    ts=t.ts,
                    type="SIGNAL_BLOCKED",
                    message=t.reason or "signal rejected by deterministic gate",
                    ref_id=t.signal_id,
                )
            )
        elif t.to_state == SignalState.EXPIRED.value:
            events.append(
                RiskEventItem(
                    ts=t.ts,
                    type="ORDER_EXPIRED",
                    message=t.reason or "pending order expired unfilled",
                    ref_id=t.signal_id,
                )
            )

    for order in fold_all(store):
        if order.state == OrderState.REJECTED:
            events.append(
                RiskEventItem(
                    ts=order.updated_at,
                    type="SIGNAL_BLOCKED",
                    asset_id=order.asset_id,
                    message=order.reason or "order rejected",
                    ref_id=order.order_id,
                )
            )

    try:
        trades = store.load_trades()
    except Exception:
        trades = []
    for rec in trades:
        if rec.exit_reason == "stop_loss":
            events.append(
                RiskEventItem(
                    ts=rec.exit_timestamp,
                    type="STOP_LOSS_TRIGGERED",
                    asset_id=rec.asset_id,
                    message=f"stop loss hit on {rec.asset_id} ({rec.net_pnl:+.2f} net)",
                    ref_id=rec.trade_id,
                )
            )
        elif rec.exit_reason == "take_profit":
            events.append(
                RiskEventItem(
                    ts=rec.exit_timestamp,
                    type="TAKE_PROFIT_REACHED",
                    asset_id=rec.asset_id,
                    message=f"take profit hit on {rec.asset_id} ({rec.net_pnl:+.2f} net)",
                    ref_id=rec.trade_id,
                )
            )

    try:
        state = store.load_account()
        if state.halted:
            events.append(
                RiskEventItem(
                    ts=state.updated_at,
                    type="EMERGENCY_HALT_ACTIVE",
                    message=state.halt_reason or "trading halted",
                    ref_id=state.account_id,
                )
            )
    except Exception:
        pass  # no account yet — empty event list is honest

    events.sort(key=lambda e: e.ts or datetime_min(), reverse=True)
    total = len(events)
    return RiskEventsPage(items=events[offset : offset + limit], total=total,
                          limit=limit, offset=offset)


def fold_all(store):
    from tradingagents.paper.models import fold_order_events

    try:
        return list(fold_order_events(store.load_order_events()).values())
    except Exception:
        return []


def datetime_min():
    from datetime import datetime, timezone

    return datetime.min.replace(tzinfo=timezone.utc)


__all__ = ["router"]
