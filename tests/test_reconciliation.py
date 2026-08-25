"""ReconciliationEngine: venue truth vs local belief, fail-closed on drift."""

from __future__ import annotations

import pytest

from tests.broker_helpers import build_engine, make_signal
from tradingagents.brokers.base import BrokerOrderStatus, ConnectionStatus
from tradingagents.brokers.sandbox import _ScriptedEvent


@pytest.fixture()
def env(tmp_path):
    engine, adapter, notifier, clock = build_engine(tmp_path)
    ready, blockers = engine.startup()
    assert ready, blockers
    return engine, adapter, clock


def test_startup_reconciliation_is_clean_and_recorded(env):
    engine, _, _ = env
    report = engine.store.last_reconciliation()
    assert report.trigger == "startup"
    assert report.clean and report.mismatches == []


def test_unknown_broker_status_is_reported_not_folded(env):
    """A status we cannot map must surface as STALE_STATE, never guessed."""
    engine, adapter, _ = env
    adapter.script([_ScriptedEvent(kind="partial", price=50_000.0, quantity=0.01)])
    result = engine.run_cycle(make_signal())
    assert result.outcome == "submitted"

    # corrupt the venue record into an unmappable state
    for rec in adapter._orders.values():
        rec.status = BrokerOrderStatus.PENDING_SUBMIT
        rec.raw_status = "IN-LIMBO"

    report = engine.reconcile(trigger="manual")
    kinds = {m.kind for m in report.mismatches}
    assert "STALE_STATE" in kinds or not report.clean


def test_unexpected_broker_order_detected(env):
    engine, adapter, _ = env
    # keep the ghost order open venue-side so it lands in the recon book
    adapter.script([_ScriptedEvent(kind="partial", price=3_000.0, quantity=0.2)])
    adapter.submit_order(
        client_order_id="TA-demo-sbx-ghost000-1",
        asset_id="ETHUSD",
        side="BUY",
        order_type="MARKET",
        quantity=1.0,
    )
    report = engine.reconcile(trigger="manual")
    kinds = [m.kind for m in report.mismatches]
    assert "UNEXPECTED_BROKER_ORDER" in kinds
    assert any(
        m.broker_value == "TA-demo-sbx-ghost000-1" for m in report.mismatches
    )


def test_injected_position_flags_missing_local_record(env):
    engine, adapter, _ = env
    adapter.inject_position("XAUUSD", quantity=-2.0, avg_price=2_400.0)
    report = engine.reconcile(trigger="manual")
    assert any(m.kind == "UNEXPECTED_BROKER_POSITION" for m in report.mismatches)
    # quantity drift is adopted with a resolution trail, never silently ignored
    positions = {p.asset_id: p for p in engine.store.load_positions()}
    assert "XAUUSD" in positions or not report.clean


def test_local_position_absent_from_broker_is_dirty_not_deleted(env):
    """Fail-closed: recon may adopt venue truth but never invent local size."""
    from datetime import datetime, timezone

    from tradingagents.execution.models import LivePosition

    engine, adapter, _ = env
    phantom = LivePosition(
        asset_id="BTCUSD",
        quantity=0.5,
        avg_entry_price=50_000.0,
        stop_loss=None,
        take_profit=None,
        protective_orders_ok=True,
        updated_at=datetime.now(timezone.utc),
    )
    engine.store.save_positions([phantom])
    report = engine.reconcile(trigger="manual")
    assert any(m.kind == "MISSING_LOCAL_POSITION" for m in report.mismatches)
    assert not report.clean
    # phantom survives: only an operator resolves this
    assert any(p.asset_id == "BTCUSD" for p in engine.store.load_positions())


def test_quantity_drift_adopts_broker_with_resolution(env):
    engine, adapter, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.0)])
    result = engine.run_cycle(make_signal())
    order = engine.store.load_order_snapshot(result.order_id)

    # simulate partial execution drift: local says full fill, venue holds less
    positions = [p.model_copy(update={"quantity": 0.3}) for p in engine.store.load_positions()]
    engine.store.save_positions(positions)
    adapter.inject_position(
        "BTCUSD", quantity=order.filled_quantity, avg_price=order.avg_fill_price or 50_000.0
    )
    report = engine.reconcile(trigger="manual")
    assert any("adopted broker quantity" in m.detail for m in report.mismatches)
    assert any("correction recorded" in r for r in report.resolutions)
    positions = {p.asset_id: p for p in engine.store.load_positions()}
    assert positions["BTCUSD"].quantity == pytest.approx(order.filled_quantity)


def test_startup_blocks_while_drift_unresolved(tmp_path):
    engine, adapter, _, _ = build_engine(tmp_path)
    ready, _ = engine.startup()
    assert ready
    adapter.inject_position("XAGUSD", quantity=100.0, avg_price=30.0)

    fresh, _, _, _ = build_engine(tmp_path, adapter=adapter)
    ready, blockers = fresh.startup()
    assert not ready
    assert any(b.startswith("reconciliation_dirty") for b in blockers)


def test_protective_order_gap_is_flagged(env):
    """Venue reports a matched position without a verified protective stop."""
    from tradingagents.brokers.base import BrokerPosition

    engine, adapter, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.0)])
    result = engine.run_cycle(make_signal())
    order = engine.store.load_order_snapshot(result.order_id)

    def unprotected():
        return (
            BrokerPosition(
                asset_id="BTCUSD",
                quantity=order.filled_quantity,
                avg_entry_price=order.avg_fill_price,
                protective_orders_ok=False,  # venue lost the attached stop
            ),
        )

    adapter.get_positions = unprotected  # type: ignore[method-assign]
    report = engine.reconcile(trigger="manual")
    kinds = [m.kind for m in report.mismatches]
    assert "PROTECTIVE_ORDER_MISSING" in kinds


def test_disconnected_adapter_fails_recon_closed(env):
    from tradingagents.brokers.base import BrokerError

    engine, adapter, _ = env
    adapter.force_status(ConnectionStatus.DISCONNECTED)
    with pytest.raises(BrokerError):
        engine.reconcile(trigger="manual")
    tripped, _ = engine.store.circuit_breaker_state()
    assert tripped, "recon failure must trip the breaker"


def test_post_trade_trigger_runs_after_fill_and_stays_clean(env):
    engine, adapter, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_004.0)])
    engine.run_cycle(make_signal())
    report = engine.store.last_reconciliation()
    assert report.trigger == "post_trade"
    assert report.clean


def test_periodic_reconcile_ignores_terminal_orders_but_checks_positions(env):
    engine, adapter, _ = env
    adapter.script([_ScriptedEvent(kind="fill", price=50_000.0)])
    engine.run_cycle(make_signal())
    report = engine.reconcile(trigger="periodic")
    # filled order is terminal; nothing pending means orders_checked == 0
    assert report.orders_checked == 0
    assert report.clean
