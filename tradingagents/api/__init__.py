"""Phase 4 web-terminal API layer (read-mostly; backend stays source of truth).

The frontend is a presentation surface over the existing Phase 1-3 engines.
This package exposes their already-computed state over HTTP/SSE and adds the
few persistence pieces the dashboard needs (research artifacts, backtest run
registry, audit log). It never re-implements research, execution, portfolio,
risk, or analytics logic — ARCHITECTURE.md P4.0-P4.8.
"""

from tradingagents.api.app import ServerSettings, create_app

__all__ = ["ServerSettings", "create_app"]
