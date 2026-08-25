"""Broker/execution endpoints (Phase 5 — sandbox adapter only).

Read-mostly like the rest of the API: the only mutations are the operator
safety controls (startup/shutdown/reconcile/halt/resume), all of which are
audited by the engine. There is deliberately NO order-submission endpoint —
signals reach execution only through the engine's own guarded cycle.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from tradingagents.api.context import AppContext
from tradingagents.api.schemas import (
    BrokerAdapterInfo,
    BrokerAdaptersResponse,
    BrokerHaltRequest,
    BrokerReconciliationReportOut,
    BrokerResumeRequest,
    BrokerShutdownResponse,
    BrokerStartupResponse,
    BrokerStatusResponse,
)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["broker"])


@router.get("/broker/adapters", response_model=BrokerAdaptersResponse)
def list_adapters() -> BrokerAdaptersResponse:
    from tradingagents.brokers.registry import available_brokers

    return BrokerAdaptersResponse(
        adapters=[
            BrokerAdapterInfo(name=name, sandbox=name == "sandbox")
            for name in available_brokers()
        ]
    )


@router.get("/broker", response_model=BrokerStatusResponse)
def broker_status(
    ctx: Annotated[AppContext, Depends(get_ctx)],
) -> BrokerStatusResponse:
    try:
        snap = ctx.broker_engine().status_snapshot()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BrokerStatusResponse(**snap)


@router.post("/broker/startup", response_model=BrokerStartupResponse)
def broker_startup(ctx: Annotated[AppContext, Depends(get_ctx)]) -> BrokerStartupResponse:
    ready, blockers = ctx.broker_engine().startup()
    return BrokerStartupResponse(ready=ready, blockers=blockers)


@router.post("/broker/shutdown", response_model=BrokerShutdownResponse)
def broker_shutdown(ctx: Annotated[AppContext, Depends(get_ctx)]) -> BrokerShutdownResponse:
    summary = ctx.broker_engine().shutdown()
    return BrokerShutdownResponse(**summary)


@router.get("/broker/reconciliation", response_model=BrokerReconciliationReportOut | None)
def last_reconciliation(
    ctx: Annotated[AppContext, Depends(get_ctx)],
) -> BrokerReconciliationReportOut | None:
    from tradingagents.execution.store import ExecutionStoreError

    try:
        report = ctx.broker_engine().store.last_reconciliation()
    except ExecutionStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if report is None:
        return None
    return BrokerReconciliationReportOut.model_validate(report.model_dump(mode="json"))


@router.post("/broker/reconcile", response_model=BrokerReconciliationReportOut)
def reconcile_now(ctx: Annotated[AppContext, Depends(get_ctx)]) -> BrokerReconciliationReportOut:
    engine = ctx.broker_engine()
    if not engine.state.started:
        raise HTTPException(status_code=409, detail="engine not started — call startup first")
    report = engine.reconcile(trigger="manual")
    return BrokerReconciliationReportOut.model_validate(report.model_dump(mode="json"))


@router.post("/broker/halt", response_model=BrokerStatusResponse)
def halt_broker(
    payload: BrokerHaltRequest,
    ctx: Annotated[AppContext, Depends(get_ctx)],
) -> BrokerStatusResponse:
    ctx.broker_engine().halt(payload.reason, operator=payload.operator)
    return BrokerStatusResponse(**ctx.broker_engine().status_snapshot())


@router.post("/broker/resume", response_model=BrokerStatusResponse)
def resume_broker(
    payload: BrokerResumeRequest,
    ctx: Annotated[AppContext, Depends(get_ctx)],
) -> BrokerStatusResponse:
    ctx.broker_engine().resume(operator=payload.operator)
    return BrokerStatusResponse(**ctx.broker_engine().status_snapshot())


__all__ = ["router"]
