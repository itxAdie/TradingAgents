"""Backtest endpoints: list, submit (audited mutation), detail + ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from tradingagents.api.backtests import (
    BacktestConfigError,
    BacktestConflictError,
    BacktestStartRequest,
)
from tradingagents.api.context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["backtests"])


@router.get("/backtests")
def list_backtests(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import BacktestJobOut, Page

    jobs, total = ctx.backtests.list_jobs(limit=limit, offset=offset)
    return Page(
        items=[BacktestJobOut(**j.model_dump(mode="json")) for j in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/backtests", status_code=202)
def start_backtest(
    request: BacktestStartRequest,
    ctx: Annotated[AppContext, Depends(get_ctx)],
):
    try:
        job = ctx.backtests.submit(request)
    except BacktestConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BacktestConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from tradingagents.api.schemas import BacktestJobOut

    return BacktestJobOut(**job.model_dump(mode="json"))


@router.get("/backtests/{run_id}")
def backtest_detail(
    run_id: str,
    ctx: Annotated[AppContext, Depends(get_ctx)],
):
    job = ctx.backtests.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    payload = {"job": job.model_dump(mode="json"), "report": None}
    if job.status == "completed" and job.report_path:
        try:
            payload["report"] = json.loads(
                Path(job.report_path).read_text(encoding="utf-8")
            )
        except OSError:
            payload["report"] = None
    return payload


@router.get("/backtests/{run_id}/ledger")
def backtest_ledger(
    run_id: str,
    ctx: Annotated[AppContext, Depends(get_ctx)],
    strategy_id: Annotated[str, Query()],
):
    path = ctx.backtests._root / f"trades_{strategy_id}_{run_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="ledger not found for strategy")
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"corrupt ledger: {exc}") from exc
    return records


__all__ = ["router"]
