"""Research endpoints: paginated run index + full stored reports/debates."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from tradingagents.api.context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["research"])


@router.get("/research")
def list_runs(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    asset_id: str | None = None,
    timeframe: str | None = None,
    action: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import Page

    rows, total = ctx.research_store.list_runs(
        asset_id=asset_id,
        timeframe=timeframe,
        action=action,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[r.model_dump(mode="json") for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/research/{run_id}")
def load_run(run_id: str, ctx: Annotated[AppContext, Depends(get_ctx)]):
    payload = ctx.research_store.load_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="research run not found")
    return payload


__all__ = ["router"]
