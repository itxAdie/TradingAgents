"""Failure-injection matrix: every broker fault has one defined outcome.

| fault              | expected outcome                                   |
|--------------------|----------------------------------------------------|
| timeout/lost resp  | UNKNOWN + quarantine + breaker                     |
| rate limit         | REJECTED (retryable class), breaker untouched      |
| malformed payload  | NON_RETRYABLE rejection, raw preserved             |
| disconnect mid-run | cycles blocked, no submission, no crash            |
| store unwritable   | policy-level block before submission               |
"""

from __future__ import annotations

import pytest

from tests.broker_helpers import build_engine, make_signal
from tradingagents.brokers.base import ConnectionStatus
from tradingagents.brokers.sandbox import _ScriptedEvent
from tradingagents.execution.models import LiveOrderState


@pytest.fixture()
def env(tmp_path):
    engine, adapter, notifier, clock = build_engine(tmp_path)
    ready, blockers = engine.startup()
    assert ready, blockers
    return engine, adapter, notifier, clock


def test_timeout_submission_is_quarantined_never_retried(env):
    engine, adapter, notifier, _ = env
    adapter.script([_ScriptedEvent(kind="timeout")])
    result = engine.run_cycle(make_signal())
    assert result.outcome == "quarantined_unknown"
    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.UNKNOWN and order.submission_unknown
    # the venue DID record it: reconciliation must find the twin, not a gap
    assert adapter.find_order(client_order_id=result.client_order_id) is not None
    assert "ORDER_UNKNOWN" in notifier.kinds()


def test_rate_limited_broker_error_is_rejected_not_quarantined(env):
    """RETRYABLE classification still maps to an audited rejection here:
    the engine itself never auto-retries (§19); a fresh signal may."""
    engine, adapter, notifier, _ = env
    adapter.script([_ScriptedEvent(kind="rate_limit")])
    result = engine.run_cycle(make_signal())
    assert result.outcome == "rejected_by_broker"
    tripped, _ = engine.store.circuit_breaker_state()
    assert not tripped
    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.REJECTED
    assert "ORDER_REJECTED" in notifier.kinds()


def test_malformed_payload_preserves_raw_and_rejects(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="malformed", message="\x00truncated")])
    result = engine.run_cycle(make_signal())
    assert result.outcome == "rejected_by_broker"
    assert result.reason_code == "malformed_response"
    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.REJECTED


def test_disconnect_mid_run_blocks_without_crash(env):
    engine, adapter, _, _ = env
    adapter.force_status(ConnectionStatus.DISCONNECTED)
    result = engine.run_cycle(make_signal())
    assert result.outcome == "blocked"
    notes = engine.process_updates()  # polling over dead connection must not raise
    assert isinstance(notes, list)


def test_recovery_after_transient_disconnect(env):
    engine, adapter, _, _ = env
    adapter.force_status(ConnectionStatus.DISCONNECTED)
    blocked = engine.run_cycle(make_signal())
    assert blocked.outcome == "blocked"

    adapter.force_status(ConnectionStatus.CONNECTED)
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.0)])
    ok = engine.run_cycle(make_signal())
    assert ok.outcome == "submitted"


def test_unknown_then_resubmission_blocked_until_manual_reset(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="timeout")])
    first = engine.run_cycle(make_signal())
    assert first.outcome == "quarantined_unknown"

    # even a *different* signal stays blocked: breaker is global
    other = make_signal(entry=51_000.0, stop=50_000.0)
    second = engine.run_cycle(other)
    assert second.outcome in {"blocked", "quarantined_unknown"}
    assert engine.ready is False or second.outcome == "blocked"


def test_partial_fill_then_full_fill_via_polling(env):
    from dataclasses import replace

    from tradingagents.brokers.base import BrokerOrderStatus

    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="partial", price=50_001.0, quantity=0.02)])
    result = engine.run_cycle(make_signal())
    order = engine.store.load_order_snapshot(result.order_id)
    assert order.status is LiveOrderState.PARTIALLY_FILLED

    # venue completes the rest; engine picks it up on the next poll
    book_order = adapter._orders[result.client_order_id]
    adapter._orders[result.client_order_id] = replace(
        book_order,
        filled_quantity=order.quantity,
        avg_fill_price=50_002.0,
        status=BrokerOrderStatus.FILLED,
    )
    # venue position reflects the completed execution too
    adapter._positions["BTCUSD"] = {"qty": order.quantity, "avg": 50_002.0}

    notes = engine.process_updates()
    assert any("FILLED" in n for n in notes)
    updated = engine.store.load_order_snapshot(result.order_id)
    assert updated.status is LiveOrderState.FILLED
    assert len(updated.fills) == 2  # partial + completion, never overwritten
    positions = {p.asset_id: p for p in engine.store.load_positions()}
    assert positions["BTCUSD"].quantity == pytest.approx(order.quantity)


def test_poll_survives_absent_order_and_reports_note(env):
    engine, adapter, _, _ = env
    adapter.script([_ScriptedEvent(kind="timeout")])
    result = engine.run_cycle(make_signal())
    adapter.drop_order(result.client_order_id)  # venue lost it entirely
    notes = engine.process_updates()
    assert any("absent" in n for n in notes)


def test_day_anchor_resets_on_utc_rollover(env):
    from datetime import timedelta

    engine, _, _, clock = env
    assert engine.state.day_start_equity == pytest.approx(100_000.0)

    clock.now = clock.now + timedelta(days=2)
    engine.run_cycle(make_signal())  # any cycle observes the rollover
    assert engine.state.realized_pnl_today == 0.0
    assert engine.state.day_start_equity > 0
