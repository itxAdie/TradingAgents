"""Portfolio endpoints: account report, equity curve, daily rows, positions.

Every number is read from the paper store via the engine's own dashboard
APIs (account_summary/build_report) — the frontend never recomputes P&L,
exposure, or drawdown (spec §14/§15/§45).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from tradingagents.api.context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["portfolio"])


@router.get("/portfolio")
def portfolio(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
):
    from tradingagents.api.schemas import PortfolioResponse

    engine = ctx.engine(environment, account)
    summary = engine.account_summary()
    report = engine.build_report()
    payload = report.model_dump(mode="json")
    payload["orders_total"] = summary["orders_total"]
    payload["open_orders"] = [
        o.model_dump(mode="json") for o in summary["open_orders"]
    ]
    return PortfolioResponse(**payload)


@router.get("/portfolio/equity-curve")
def equity_curve(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 2000,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import EquityCurveResponse

    rows = ctx.store(environment, account).load_equity_curve()
    if date_from:
        rows = [r for r in rows if r.timestamp >= date_from]
    if date_to:
        rows = [r for r in rows if r.timestamp <= date_to]
    total = len(rows)
    page = rows[offset : offset + limit]  # chronological order preserved
    return EquityCurveResponse(items=page, total=total, limit=limit, offset=offset)


@router.get("/portfolio/daily")
def daily_rows(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import DailyRowsResponse

    rows = ctx.store(environment, account).load_daily_folded()  # ascending by date
    total = len(rows)
    page = rows[offset : offset + limit]
    return DailyRowsResponse(items=page, total=total, limit=limit, offset=offset)


@router.get("/portfolio/positions")
def positions(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
    asset_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import PositionOut, PositionsResponse

    records = ctx.store(environment, account).load_positions()
    if asset_id:
        wanted = asset_id.strip().upper()
        records = [r for r in records if r.asset_id == wanted]
    records.sort(key=lambda r: r.entry_time, reverse=True)
    total = len(records)
    page = records[offset : offset + limit]
    items = [
        PositionOut(
            **r.model_dump(),
            unrealized_pnl=r.unrealized_pnl,
        )
        for r in page
    ]
    return PositionsResponse(items=items, total=total, limit=limit, offset=offset)


__all__ = ["router"]
