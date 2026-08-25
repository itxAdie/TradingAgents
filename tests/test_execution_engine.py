"""LiveExecutionEngine lifecycle: the guarded path from signal to venue.

Every test runs fully offline against the sandbox adapter and a scripted
provider; every failure mode must end in a *blocked* cycle, an audited
rejection or an UNKNOWN quarantine — never a retry, never a crash.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.broker_helpers import (
    RecordingNotifier,
    SteppingClock,
    build_engine,
    make_signal,
)
from tradingagents.brokers.base import ConnectionStatus
from tradingagents.brokers.sandbox import _ScriptedEvent
from tradingagents.execution.models import LiveOrderState
from tradingagents.research.schemas import SignalAction

pytest.importorskip("tradingagents.execution.engine")


@pytest.fixture()
def env(tmp_path):
    engine, adapter, notifier, clock = build_engine(tmp_path)
    ready, blockers = engine.startup()
    assert ready, blockers
    return engine, adapter, notifier, clock


def test_startup_ready_and_reconciled(env):
    engine, _, _, _ = env
    report = engine.store.last_reconciliation()
    assert report is not None and report.clean
    assert engine.ready


def test_startup_blocks_on_account_identity_mismatch(tmp_path):
    engine, adapter, _, _ = build_engine(tmp_path, account_id="configured-acc")
    # venue reports a different account than configured → hard blocker
    original_get_account = adapter.get_account

    def lying_account():
        snap = original_get_account()
        return type(snap)(
            account_id="someone-else",
            currency=snap.currency,
            cash=snap.cash,
            equity=snap.equity,
            positions=snap.positions,
            open_orders=snap.open_orders,
            server_time=snap.server_time,
            leverage_cap=snap.leverage_cap,
        )

    adapter.get_account = lying_account  # type: ignore[method-assign]
    ready, blockers = engine.startup()
    assert not ready
    assert any(b.startswith("account_mismatch") for b in blockers)


def test_hold_signal_is_a_no_trade_cycle(env):
    engine, _, notifier, _ = env
    result = engine.run_cycle(make_signal(action=SignalAction.HOLD))
    assert result.outcome == "no_trade"
    assert engine.store.all_order_snapshots() == []
    assert "ORDER_REJECTED" not in notifier.kinds()


def test_happy_path_fill_with_slippage_and_clean_post_trade_recon(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_010.0)])
    result = engine.run_cycle(make_signal())
    assert result.outcome == "submitted"

    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.FILLED
    assert order.submitted_at is not None and order.filled_at is not None
    # risk-budget sizing: 100k equity × 0.5% × 0.9999 ÷ 2% stop ÷ 50_000 entry
    assert order.quantity == pytest.approx(0.49995, abs=1e-6)
    # slippage vs requested reference: (50010 - 50000)/50000 = 2 bps
    assert order.fills[0].slippage_bps == pytest.approx(2.0, abs=1e-6)
    # local position cache updated so post-trade reconciliation compares real state
    positions = {p.asset_id: p for p in engine.store.load_positions()}
    assert positions["BTCUSD"].quantity == pytest.approx(order.quantity)
    report = engine.store.last_reconciliation()
    assert report.trigger == "post_trade"
    assert report.clean


def test_lifecycle_events_are_append_only_and_ordered(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.5)])
    result = engine.run_cycle(make_signal())
    events = engine.store.order_events()
    states = [e.to_state.value for e in events if e.order_id == result.order_id]
    assert states == ["RISK_APPROVED", "SUBMITTED", "FILLED"]


def test_risk_budget_sizing_capped_by_max_order_value(tmp_path):
    engine, adapter, _, _ = build_engine(
        tmp_path, limits_overrides={"max_order_value": 1_000.0}
    )
    ready, blockers = engine.startup()
    assert ready, blockers
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.0)])
    result = engine.run_cycle(make_signal())
    order = engine.store.load_order_snapshot(result.order_id)
    # value cap: 1_000 / 50_000 = 0.02 beats the risk-budget quantity
    assert order.quantity == pytest.approx(0.02, abs=1e-9)


def test_disconnected_broker_blocks_before_submission(env):
    engine, adapter, _, _ = env
    adapter.force_status(ConnectionStatus.DISCONNECTED)
    result = engine.run_cycle(make_signal())
    assert result.outcome == "blocked"
    assert result.reason_code == "broker_not_connected"


def test_stale_market_data_blocks_the_cycle(tmp_path):
    from tests.broker_helpers import FakeProvider

    clock = SteppingClock()
    # last closed bar 30 days old: far beyond the H1 freshness window (7d)
    stale = FakeProvider(bar_close=clock.now - timedelta(days=30))
    engine, _, _, _ = build_engine(tmp_path, provider=stale)
    ready, blockers = engine.startup()
    assert ready, blockers
    result = engine.run_cycle(make_signal())
    assert result.outcome == "blocked"
    assert result.reason_code == "stale_market_data"


def test_missing_market_data_fails_closed(tmp_path):
    class DeadProvider:
        name = "dead"

        def get_ohlcv(self, *a, **k):
            raise RuntimeError("feed down")

        def get_quote(self, *a):
            return None

    engine, _, _, _ = build_engine(tmp_path, provider=DeadProvider())
    ready, blockers = engine.startup()
    assert ready, blockers
    result = engine.run_cycle(make_signal())
    assert result.outcome == "blocked"
    assert result.reason_code == "market_data_missing"


def test_unknown_timeout_quarantines_and_trips_breaker(env):
    engine, adapter, notifier, _ = env
    adapter.script([_ScriptedEvent(kind="timeout")])
    signal = make_signal()
    result = engine.run_cycle(signal)
    assert result.outcome == "quarantined_unknown"

    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.UNKNOWN
    assert order.quarantined and order.submission_unknown
    tripped, reason = engine.store.circuit_breaker_state()
    assert tripped and "unknown" in reason.lower()

    # same decision content cannot re-enter while unresolved
    again = engine.run_cycle(signal)
    assert again.outcome == "blocked"
    assert again.reason_code == "signal_quarantined"

    assert "ORDER_UNKNOWN" in notifier.kinds()
    assert "CIRCUIT_BREAKER_TRIGGERED" in notifier.kinds()


def test_reconciliation_resolves_unknown_to_acknowledged(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="timeout")])
    result = engine.run_cycle(make_signal())

    # breaker stays tripped even though reconciliation resolves the fold:
    # manual reset is the only way back (spec §20).
    report = engine.reconcile(trigger="manual")
    assert report.clean, [m.detail for m in report.mismatches]
    assert any("UNKNOWN -> ACKNOWLEDGED" in r for r in report.resolutions)
    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.ACKNOWLEDGED
    assert engine.store.circuit_breaker_state()[0] is True


def test_in_flight_signal_blocked_across_restart(tmp_path):
    """Restart safety: a fresh engine over the same store sees the open order."""
    engine, adapter, _, clock = build_engine(tmp_path)
    ready, blockers = engine.startup()
    assert ready, blockers
    adapter.script([_ScriptedEvent(kind="partial", price=50_001.0, quantity=0.01)])
    first = engine.run_cycle(make_signal())
    assert first.outcome == "submitted"

    # venue persists; only the engine + its store view restart
    fresh_engine, _, _, _ = build_engine(tmp_path, adapter=adapter, clock=clock)
    ready, blockers = fresh_engine.startup()
    assert ready, blockers
    again = fresh_engine.run_cycle(make_signal())
    assert again.outcome == "blocked"
    assert again.reason_code == "signal_in_flight"
    assert "-1" in (again.detail)  # references the seq-1 client id


def test_terminal_prior_submission_allows_new_sequence(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_002.0)])
    first = engine.run_cycle(make_signal())
    assert first.client_order_id.endswith("-1")

    # same content re-delivered after terminal fill: deliberate re-entry gets seq 2
    second = engine.run_cycle(make_signal())
    assert second.outcome == "submitted"
    assert second.client_order_id.endswith("-2")
    assert first.client_order_id != second.client_order_id


def test_broker_rejection_does_not_trip_breaker(env):
    engine, adapter, notifier, _ = env
    adapter.script(
        [_ScriptedEvent(kind="reject", code="insufficient_margin", message="nope")]
    )
    result = engine.run_cycle(make_signal())
    assert result.outcome == "rejected_by_broker"
    assert result.reason_code == "insufficient_margin"
    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.REJECTED
    tripped, _ = engine.store.circuit_breaker_state()
    assert tripped is False
    assert "ORDER_REJECTED" in notifier.kinds()


def test_consecutive_losses_auto_halt_persistently(tmp_path):
    engine, adapter, _, _ = build_engine(
        tmp_path, limits_overrides={"max_consecutive_losses": 1}
    )
    ready, blockers = engine.startup()
    assert ready, blockers

    # open long (fill), then close it below entry → one realized loss
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.0)])
    opened = engine.run_cycle(make_signal())
    assert opened.outcome == "submitted"

    close_signal = make_signal(action=SignalAction.SELL, entry=50_000.0, stop=51_000.0)
    adapter.script([_ScriptedEvent(kind="fill", price=49_000.0)])
    closed = engine.run_cycle(close_signal)
    assert closed.outcome == "submitted"
    assert engine.state.consecutive_losses >= 1

    halted, halt_reason = engine.store.circuit_breaker_state()[0], None
    del halted, halt_reason
    # next BUY attempt must be vetoed by the consecutive-loss rule (and halt persisted)
    result = engine.run_cycle(make_signal())
    assert result.outcome == "blocked"
    assert result.reason_code == "max_consecutive_losses"
    halted_now, halt_reason = engine.store.is_halted()
    assert halted_now and "consecutive" in halt_reason.lower() or "loss" in halt_reason.lower()


def test_manual_halt_and_operator_resume_roundtrip(env):
    engine, _, _, _ = env
    engine.halt("drill", operator="op-1")
    halted, reason = engine.store.is_halted()
    assert halted and reason == "drill"
    blocked = engine.run_cycle(make_signal())
    assert blocked.reason_code == "trading_halted"

    engine.resume(operator="op-2")
    halted, _ = engine.store.is_halted()
    assert not halted


def test_metrics_capture_latencies_and_counters(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.0)])
    result = engine.run_cycle(make_signal())
    metrics = engine.metrics
    assert metrics.counter("cycles_total") >= 1
    assert metrics.counter("orders_submitted_total") == 1
    stats = metrics.sample_stats("submit_latency_s")
    assert stats is not None and stats["count"] == 1
    assert engine.metrics.get_gauge("equity") > 0
    assert metrics.heartbeat_age("execution_engine") is not None
    del result


def test_status_snapshot_never_raises_and_reports_truth(env):
    engine, adapter, _, _ = env
    adapter.force_status(ConnectionStatus.RATE_LIMITED)
    snap = engine.status_snapshot()
    assert snap["connection"] == "RATE_LIMITED"
    assert snap["environment"] == "demo"
    assert snap["live_armed"] is False  # sandbox can never arm live


def test_notifier_rejects_unknown_alert_names():
    from tradingagents.execution.alerts import AlertEmitter

    emitter = AlertEmitter(notifier=RecordingNotifier())
    with pytest.raises(ValueError, match="unknown alert"):
        emitter.emit("NOT_A_REAL_ALERT", {})
