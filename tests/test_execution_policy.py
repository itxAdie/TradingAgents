"""ExecutionPolicy: ordered fail-closed pre-flight chain (§42)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.brokers.base import BrokerAccountSnapshot, ConnectionStatus
from tradingagents.execution.config import load_live_execution_config
from tradingagents.execution.policy import ExecutionPolicy
from tradingagents.execution.store import ExecutionStore


def _account(account_id="sbx-test", equity=100_000.0):
    return BrokerAccountSnapshot(
        account_id=account_id,
        currency="USD",
        cash=equity,
        equity=equity,
        positions=(),
        open_orders=(),
        server_time=None,
        leverage_cap=None,
    )


@pytest.fixture()
def policy(tmp_path):
    config = load_live_execution_config(
        broker_name="sandbox",
        cache_dir=tmp_path / "cache",
        account_id="sbx-test",
    )
    store = ExecutionStore(root=tmp_path / "store")
    return ExecutionPolicy(config=config, store=store), store, config


def _kwargs(**kw):
    base = {
        "connection": ConnectionStatus.CONNECTED,
        "adapter_healthy": True,
        "account": _account(),
        "last_bar_close": datetime.now(timezone.utc) - timedelta(hours=1),
        "timeframe": "1h",
    }
    base.update(kw)
    return base


def test_clean_state_allows(policy):
    p, store, _ = policy
    store.append_reconciliation(_clean_report())
    decision = p.check(**_kwargs())
    assert decision.allowed, decision.reason_code


def test_reconciliation_required_before_first_cycle(policy):
    p, _, _ = policy
    decision = p.check(**_kwargs())
    assert not decision.allowed
    assert decision.reason_code == "reconciliation_required"


def test_dirty_reconciliation_blocks(policy):
    from tradingagents.execution.models import ReconciliationMismatch, ReconciliationReport

    p, store, _ = policy
    report = ReconciliationReport(
        ts=datetime.now(timezone.utc),
        trigger="startup",
        orders_checked=0,
        positions_checked=0,
        clean=False,
        mismatches=[
            ReconciliationMismatch(kind="MISSING_LOCAL_POSITION", detail="x"),
        ],
        resolutions=[],
    )
    store.append_reconciliation(report)
    decision = p.check(**_kwargs())
    assert not decision.allowed
    assert decision.reason_code == "reconciliation_incomplete"


def test_halt_beats_breaker_in_ordering(policy):
    p, store, _ = policy
    store.append_reconciliation(_clean_report())
    store.set_halt(True, "operator")
    store.set_circuit_breaker(True, "boom")
    d = p.check(**_kwargs())
    assert d.reason_code == "trading_halted"

    store.set_halt(False, "manual_reset:op")
    d = p.check(**_kwargs())
    assert d.reason_code == "circuit_breaker"


def test_account_identity_mismatch_blocks(policy):
    p, store, _ = policy
    store.append_reconciliation(_clean_report())
    d = p.check(**_kwargs(account=_account(account_id="someone-else")))
    assert not d.allowed and d.reason_code == "account_identity_mismatch"


def test_stale_and_missing_market_data_block(policy):
    p, store, _ = policy
    store.append_reconciliation(_clean_report())

    d = p.check(**_kwargs(last_bar_close=None))
    assert not d.allowed and d.reason_code == "market_data_missing"

    old = datetime.now(timezone.utc) - timedelta(days=30)
    d = p.check(**_kwargs(last_bar_close=old))
    assert not d.allowed and d.reason_code == "stale_market_data"


def test_store_unavailable_fails_closed(policy):
    p, _, _ = policy
    d = p.check(**_kwargs(store_writable=False))
    assert not d.allowed and d.reason_code == "store_unavailable"


def test_degraded_health_blocks_even_when_connected(policy):
    p, store, _ = policy
    store.append_reconciliation(_clean_report())
    d = p.check(**_kwargs(adapter_healthy=False))
    assert not d.allowed and d.reason_code == "broker_degraded"


def test_unknown_connection_state_blocks(policy):
    p, store, _ = policy
    store.append_reconciliation(_clean_report())
    d = p.check(**_kwargs(connection=ConnectionStatus.UNKNOWN))
    assert not d.allowed and d.reason_code == "broker_not_connected"


def test_internal_failure_reports_policy_error(tmp_path):
    config = load_live_execution_config(
        broker_name="sandbox", cache_dir=tmp_path / "c", account_id="a"
    )

    class ExplodingStore(ExecutionStore):
        def is_halted(self, *a, **k):
            raise RuntimeError("disk on fire")

    p = ExecutionPolicy(config=config, store=ExplodingStore(root=tmp_path / "s"))
    d = p.check(**_kwargs())
    assert not d.allowed and d.reason_code in {"policy_error", "trading_halted"}


def _clean_report():
    from tradingagents.execution.models import ReconciliationReport

    return ReconciliationReport(
        ts=datetime.now(timezone.utc),
        trigger="startup",
        orders_checked=0,
        positions_checked=0,
        clean=True,
        mismatches=[],
        resolutions=[],
    )
