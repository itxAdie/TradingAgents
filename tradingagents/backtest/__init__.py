"""Phase 2 — backtesting engine (research-only simulation).

This package replays the Phase 1 research engine over historical data and
evaluates the resulting :class:`~tradingagents.research.schemas.ResearchSignal`
stream with a deterministic simulated executor. It reuses the existing
market-data models, asset registry, timeframes, indicators, agents, schemas,
configuration, logging, and error taxonomy — it never duplicates signal
generation and never connects to a broker.

Layer position: ``backtest/`` may import from ``marketdata/``, ``assets/``,
``analysis/``, ``research/`` and ``dataflows/``; no lower layer may import
from this package.
"""
