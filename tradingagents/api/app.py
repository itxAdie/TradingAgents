"""FastAPI application factory for the Phase 4 web terminal.

Binds loopback by default, serves the built dashboard from
``dashboard/dist`` when present, exposes JSON APIs under ``/api/*`` and an
SSE stream at ``/api/events/stream``. Optional shared-token auth via the
``TRADINGAGENTS_API_TOKEN`` env var (spec §33/§34). The backend remains the
single source of truth — no trading logic lives in any client.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from tradingagents.api.backtests import BacktestConfigError, BacktestConflictError
from tradingagents.api.context import AppContext
from tradingagents.paper.store import PaperStateError

logger = logging.getLogger("tradingagents.api")

_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


class ServerSettings(BaseModel):
    """Everything the API server needs to run; built from CLI flags."""

    environment: str = "test"
    account_id: str = "paper-default"
    broker_name: str = "sandbox"  # only registered adapter in Phase 5
    assets: list[str] = Field(default_factory=lambda: ["XAUUSD", "BTCUSD"])
    timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h", "1d"])
    enable_research_loop: bool = False  # kill switch — arming is audited
    quote_poll_seconds: int = Field(default=30, ge=0, le=3600)
    dashboard_dir: Path | None = None
    api_token: str | None = None


def _error(code: str, message: str, status: int, detail=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )


def create_app(settings: ServerSettings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx: AppContext = app.state.ctx
        ctx.start_background_workers()
        try:
            yield
        finally:
            ctx.shutdown_background_workers()

    app = FastAPI(
        title="TradingAgents API",
        version="0.4.0",
        description=(
            "Read-mostly web-terminal API over the TradingAgents research/"
            "backtest/paper engines. PAPER TRADING - SIMULATED EXECUTION ONLY."
        ),
        lifespan=lifespan,
    )
    ctx = AppContext(settings)
    app.state.ctx = ctx

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- optional shared-token guard (multi-user/RBAC-ready seam) --------------

    @app.middleware("http")
    async def token_guard(request: Request, call_next):
        if settings.api_token and request.url.path.startswith("/api"):
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {settings.api_token}":
                return _error("unauthorized", "missing or invalid bearer token", 401)
        return await call_next(request)

    # -- uniform error envelopes (spec §26/§32) ---------------------------------

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return _error("invalid_request", "request validation failed", 422, exc.errors())

    @app.exception_handler(PaperStateError)
    async def paper_state_handler(request: Request, exc: PaperStateError):
        if "no paper account" in str(exc):
            return _error("no_account", str(exc), 404)
        return _error("paper_state_error", str(exc), 409)

    @app.exception_handler(BacktestConfigError)
    async def backtest_config_handler(request: Request, exc: BacktestConfigError):
        return _error("invalid_backtest", str(exc), 400)

    @app.exception_handler(BacktestConflictError)
    async def backtest_conflict_handler(request: Request, exc: BacktestConflictError):
        return _error("backtest_busy", str(exc), 409)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        logger.exception("unhandled API error on %s", request.url.path)
        return _error("internal_error", "unexpected server error", 500)

    # -- routers -----------------------------------------------------------------

    from tradingagents.api.routes import (
        backtests,
        broker,
        events,
        markets,
        portfolio,
        research,
        risk,
        signals,
        system,
        trades,
    )

    api_prefix = "/api"
    app.include_router(markets.router, prefix=api_prefix)
    app.include_router(research.router, prefix=api_prefix)
    app.include_router(signals.router, prefix=api_prefix)
    app.include_router(portfolio.router, prefix=api_prefix)
    app.include_router(trades.router, prefix=api_prefix)
    app.include_router(risk.router, prefix=api_prefix)
    app.include_router(backtests.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)
    app.include_router(events.router, prefix=api_prefix)
    app.include_router(broker.router, prefix=api_prefix)

    # -- SPA static hosting -------------------------------------------------------

    dashboard = settings.dashboard_dir
    index_path = (dashboard / "index.html") if dashboard else None
    if index_path and index_path.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.exceptions import HTTPException as StarletteHTTPException

        app.mount("/", StaticFiles(directory=str(dashboard), html=True), name="dashboard")

        @app.exception_handler(StarletteHTTPException)
        async def spa_fallback(request: Request, exc: StarletteHTTPException):
            """History-API fallback: unknown non-/api paths render the SPA."""
            if (
                exc.status_code == 404
                and not request.url.path.startswith(api_prefix)
                and request.method in ("GET", "HEAD")
                and index_path is not None
            ):
                return FileResponse(index_path)
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": {"code": "http_error", "message": str(exc.detail)}},
            )

    else:

        @app.get("/", include_in_schema=False)
        def root() -> dict:
            return {
                "service": "TradingAgents API",
                "mode": "PAPER TRADING - SIMULATED EXECUTION ONLY",
                "docs": "/docs",
                "dashboard": "not built (see ACCESS_INFO.md)",
            }

    return app


__all__ = ["ServerSettings", "create_app"]
