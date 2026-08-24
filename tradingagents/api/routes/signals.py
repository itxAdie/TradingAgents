"""Signal endpoints: history with filters + full research-attributed detail."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from tradingagents.api.context import AppContext
from tradingagents.paper.models import SignalState, fold_order_events


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["signals"])


@router.get("/signals")
def list_signals(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
    asset_id: str | None = None,
    action: str | None = None,
    timeframe: str | None = None,
    state: str | None = Query(None, description="generated|accepted|rejected|executed|expired|superseded"),
    confidence_min: float | None = Query(None, ge=0, le=1),
    confidence_max: float | None = Query(None, ge=0, le=1),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import SignalListItem, SignalListPage

    store = ctx.store(environment, account)
    records = store.list_signals()

    if asset_id:
        wanted = asset_id.strip().upper()
        records = [r for r in records if r.asset_id == wanted]
    if action:
        records = [r for r in records if r.action.value == action.strip().upper()]
    if timeframe:
        records = [r for r in records if r.timeframe == timeframe.strip().lower()]
    if state:
        records = [
            r for r in records if r.state.value == state.strip().lower()
        ]
    if confidence_min is not None:
        records = [r for r in records if r.confidence >= confidence_min]
    if confidence_max is not None:
        records = [r for r in records if r.confidence <= confidence_max]
    if date_from:
        records = [r for r in records if r.generated_at >= date_from]
    if date_to:
        records = [r for r in records if r.generated_at <= date_to]

    records.sort(key=lambda r: r.generated_at, reverse=True)
    total = len(records)
    page = records[offset : offset + limit]

    all_transitions = store.list_signal_transitions()
    by_signal: dict[str, list] = {}
    for t in all_transitions:
        by_signal.setdefault(t.signal_id, []).append(t)

    items = []
    for rec in page:
        risk_decision = ""
        for t in by_signal.get(rec.signal_id, []):
            if t.to_state == SignalState.REJECTED.value:
                risk_decision = f"rejected:{t.reason}"
                break
            if t.to_state == SignalState.ACCEPTED.value:
                risk_decision = "approved"
        items.append(
            SignalListItem(
                signal_id=rec.signal_id,
                asset_id=rec.asset_id,
                timeframe=rec.timeframe,
                state=rec.state.value,
                action=rec.action.value,
                confidence=rec.confidence,
                generated_at=rec.generated_at,
                updated_at=rec.updated_at,
                rejection_reason=rec.rejection_reason,
                executed=rec.state == SignalState.EXECUTED,
                risk_decision=risk_decision,
            )
        )
    return SignalListPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/signals/{signal_id}")
def signal_detail(
    signal_id: str,
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
):
    from tradingagents.api.schemas import (
        ResearchRunRef,
        SignalDetailResponse,
        SignalTransitionRow,
    )

    store = ctx.store(environment, account)
    record = store.load_signal(signal_id)
    if record is None:
        raise HTTPException(status_code=404, detail="signal not found")

    transitions = [
        SignalTransitionRow(**t.model_dump())
        for t in store.list_signal_transitions()
        if t.signal_id == signal_id
    ]
    orders = [
        o
        for o in fold_order_events(store.load_order_events()).values()
        if o.signal_id == signal_id
    ]

    # Attribute the stored research run (report/debate) to this signal by
    # asset+timeframe+action with a small generation-time tolerance — never
    # regenerated, only matched against persisted artifacts.
    runs, _ = ctx.research_store.list_runs(
        asset_id=record.asset_id, timeframe=record.timeframe, limit=50
    )
    research_run = None
    best_delta: Any = None
    for run in runs:
        if run.signal_action != record.action.value:
            continue
        delta = abs((run.generated_at - record.generated_at).total_seconds())
        if delta <= 5.0 and (best_delta is None or delta < best_delta):
            best_delta = delta
            research_run = ResearchRunRef(
                run_id=run.run_id,
                generated_at=run.generated_at,
                signal_action=run.signal_action,
                confidence=run.confidence,
            )

    return SignalDetailResponse(
        record=record,
        transitions=transitions,
        orders=orders,
        research_run=research_run,
    )


__all__ = ["router"]
