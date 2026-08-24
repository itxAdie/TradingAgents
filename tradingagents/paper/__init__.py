"""Paper-trading package (Phase 3).

Real-time paper trading that reuses the Phase 1 research engine and the
Phase 2 execution/portfolio/ledger/analytics stack unchanged, drives them
with actual current market time, and persists every artifact so the system
survives restarts.

SAFETY: this package is 100% simulated. There is no broker connectivity, no
order placement, and no live-account credential handling anywhere under
``tradingagents/paper/`` (enforced by ``tests/test_paper_safety.py``). The
only executor in the system is
:class:`tradingagents.backtest.execution.ExecutionSimulator` — pure
arithmetic over bar data. A future ``BrokerExecution`` (Phase 5) must be a
separate class behind an execution interface; it must never be imported
here (ARCHITECTURE.md P3.2).
"""
