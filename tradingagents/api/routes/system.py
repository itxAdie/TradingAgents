"""System status + audit trail endpoints (spec §36/§44)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from tradingagents.api.context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["system"])


@router.get("/system/status")
def system_status(ctx: Annotated[AppContext, Depends(get_ctx)]):
    from tradingagents.api.schemas import SystemStatusItem, SystemStatusResponse
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

    components: list[SystemStatusItem] = [
        SystemStatusItem(component="Backend", status="online", detail="api server running")
    ]

    # Market data: provider name + last quote poll outcome.
    md_detail = "no polls yet"
    md_status = "idle"
    if ctx.last_quote_poll:
        newest = max(ctx.last_quote_poll.values(), key=lambda q: q["ts"])
        md_status = "online"
        md_detail = (
            f"last quote {newest['asset_id']} @ {newest['ts']} "
            f"(source {newest['source']}, {newest['data_status']})"
        )
    if ctx.quote_poll_failures:
        md_status = "degraded"
        md_detail += f"; {ctx.quote_poll_failures} failed polls"
    components.append(
        SystemStatusItem(
            component="Market Data", status=md_status, detail=md_detail  # type: ignore[arg-type]
        )
    )

    # AI research: configured models + whether any provider key is present.
    keys_present = sorted(
        {
            env_var
            for env_var in set(PROVIDER_API_KEY_ENV.values())
            if env_var and __import__("os").environ.get(env_var)
        }
    )[:5]
    ai_configured = bool(keys_present)
    components.append(
        SystemStatusItem(
            component="AI Research",
            status="enabled" if ai_configured else "disabled",
            detail=(
                f"keys present for: {', '.join(k[:-8] + '***' for k in keys_present)}"
                if keys_present
                else "no provider API key in environment"
            ),
        )
    )

    # Database == paper JSON store root.
    db_ok = ctx.paper_root.exists() or ctx.paper_root.parent.exists()
    components.append(
        SystemStatusItem(
            component="Database",
            status="online" if db_ok else "offline",
            detail=str(ctx.paper_root),
        )
    )

    cfg = ctx.server_config()
    loop_alive = ctx._loop_thread is not None and ctx._loop_thread.is_alive()
    store = ctx.store()
    next_runs = []
    for entry in cfg.schedules:
        state = store.load_schedule_state(f"{entry.asset_id.upper()}:{entry.timeframe}") or {}
        if state.get("next_run_at"):
            next_runs.append(state["next_run_at"])
    components.append(
        SystemStatusItem(
            component="Scheduler",
            status="online" if loop_alive else ("disabled" if not cfg.enabled else "offline"),
            detail=(
                f"next run {min(next_runs)} UTC" if next_runs else "research loop not armed"
            ),
        )
    )

    try:
        account = store.load_account()
        paper_status = "disabled" if not cfg.enabled else ("offline" if account.halted else "enabled")
        paper_detail = (
            f"account {account.account_id} [{account.environment}]"
            + (f" HALTED: {account.halt_reason}" if account.halted else "")
        )
    except Exception:
        paper_status = "idle"
        paper_detail = "no paper account initialised"
    components.append(SystemStatusItem(component="Paper Trading", status=paper_status, detail=paper_detail))  # type: ignore[arg-type]

    components.append(
        SystemStatusItem(
            component="Realtime Connection",
            status="online",
            detail=f"{ctx.bus.subscriber_count} SSE subscriber(s); "
            f"quote poll every {ctx.settings.quote_poll_seconds}s",
        )
    )

    degraded = any(c.status in ("offline", "degraded") for c in components)
    return SystemStatusResponse(
        overall="degraded" if degraded else "online",
        components=components,
        generated_at=datetime.now(timezone.utc),
    )


@router.get("/system/settings")
def server_settings_view(ctx: Annotated[AppContext, Depends(get_ctx)]):
    """Safe configuration subset (spec §37) — never includes secrets."""
    cfg = ctx.server_config()
    from tradingagents.default_config import DEFAULT_CONFIG

    return {
        "environment": cfg.environment,
        "account_id": cfg.account_id,
        "trading_enabled": cfg.enabled,
        "assets": sorted({s.asset_id for s in cfg.schedules}),
        "timeframes": sorted({s.timeframe for s in cfg.schedules}),
        "risk_limits": cfg.risk.model_dump(),
        "execution": cfg.execution.model_dump(),
        "initial_capital": cfg.initial_capital,
        "models": {
            "quick_think_llm": DEFAULT_CONFIG.get("quick_think_llm"),
            "deep_think_llm": DEFAULT_CONFIG.get("deep_think_llm"),
        },
        "quote_poll_seconds": ctx.settings.quote_poll_seconds,
        "enable_macro": cfg.enable_macro,
    }


@router.get("/system/audit")
def audit_tail(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import AuditEventRow, AuditLogResponse

    rows = ctx.audit.tail(limit=limit + offset)
    page = rows[::-1][offset : offset + limit]  # newest first
    total = len(rows)
    return AuditLogResponse(
        items=[AuditEventRow(**r) for r in page], total=total, limit=limit, offset=offset
    )


__all__ = ["router"]
