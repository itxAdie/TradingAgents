"""HardRiskGate: every limit is a hard veto, every unknown fails closed."""

from __future__ import annotations

import pytest

from tradingagents.brokers.base import (
    BrokerAccountSnapshot,
    BrokerPosition,
    ConnectionStatus,
)
from tradingagents.execution.config import LiveRiskLimits
from tradingagents.execution.gate import CircuitBreaker, HardRiskGate
from tradingagents.execution.store import ExecutionStore
from tradingagents.research.schemas import SignalAction


def _account(**kw):
    base = {
        "account_id": "sbx-test",
        "currency": "USD",
        "cash": 100_000.0,
        "equity": 100_000.0,
        "positions": (),
        "open_orders": (),
        "server_time": None,
        "leverage_cap": None,
    }
    base.update(kw)
    return BrokerAccountSnapshot(**base)


def _kwargs(**kw):
    base = {
        "action": SignalAction.BUY,
        "asset_id": "BTCUSD",
        "quantity": 0.1,
        "reference_price": 50_000.0,
        "stop_distance_pct": 2.0,
        "account": _account(),
        "connection": ConnectionStatus.CONNECTED,
        "open_orders_count": 0,
        "consecutive_losses": 0,
        "day_start_equity": 100_000.0,
        "peak_equity": 100_000.0,
        "realized_pnl_today": 0.0,
    }
    base.update(kw)
    return base


@pytest.fixture()
def gate(tmp_path):
    store = ExecutionStore(root=tmp_path / "store")
    limits = LiveRiskLimits(
        max_leverage=5.0,
        max_total_exposure_pct=100.0,
        max_order_value=1_000_000.0,
        max_position_notional=1_000_000.0,
    )
    breaker = CircuitBreaker(store=store)
    return HardRiskGate(limits=limits, store=store, breaker=breaker), store, limits


def test_allows_order_inside_all_limits(gate):
    g, _, _ = gate
    assert g.evaluate(**_kwargs()).approved


def test_halt_and_breaker_block_first_with_distinct_codes(gate):
    g, store, _ = gate
    store.set_halt(True, "operator halt")
    d = g.evaluate(**_kwargs())
    assert not d.approved and d.reason_code == "trading_halted"

    store.set_halt(False, "manual_reset:op")
    store.set_circuit_breaker(True, "boom")
    d = g.evaluate(**_kwargs())
    assert not d.approved and d.reason_code == "circuit_breaker"


def test_infrastructure_failures_veto_before_limits(gate):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(connection=ConnectionStatus.DISCONNECTED))
    assert not d.approved and d.reason_code == "broker_not_connected"
    d = g.evaluate(**_kwargs(account=None))
    assert not d.approved and d.reason_code == "account_unavailable"
    d = g.evaluate(**_kwargs(account=_account(equity=-1.0)))
    assert not d.approved and d.reason_code == "account_unhealthy"


def test_hold_is_allowed_without_further_checks(gate):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(action=SignalAction.HOLD, realized_pnl_today=None))
    assert d.approved


def test_daily_loss_veto_at_limit(gate):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(realized_pnl_today=-2_000.0))  # 2% of anchor
    assert not d.approved and d.reason_code == "max_daily_loss"


@pytest.mark.parametrize("bad", [None, 0.0, -5.0])
def test_day_anchor_unknown_fails_closed(gate, bad):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(day_start_equity=bad))
    assert not d.approved and d.reason_code == "day_anchor_unknown"


def test_daily_pnl_unknown_fails_closed(gate):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(realized_pnl_today=None))
    assert not d.approved and d.reason_code == "daily_pnl_unknown"


def test_drawdown_veto_persists_a_halt(gate):
    g, store, _ = gate
    d = g.evaluate(**_kwargs(peak_equity=120_000.0))  # 16.7% off peak >= 10% limit
    assert not d.approved and d.reason_code == "max_drawdown"
    halted, reason = store.is_halted()
    assert halted and "drawdown" in reason.lower()


def test_peak_unknown_fails_closed(gate):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(peak_equity=None))
    assert not d.approved and d.reason_code == "peak_unknown"


def test_consecutive_losses_trip_halt_then_block_next(gate):
    g, store, _ = gate
    d = g.evaluate(**_kwargs(consecutive_losses=3))
    assert not d.approved and d.reason_code == "max_consecutive_losses"
    halted, reason = store.is_halted()
    assert halted and "consecutive" in reason.lower()


def test_leverage_cap_includes_venue_maximum(gate):
    g, _, _ = gate
    # 2 units @50k = 100k notional on 100k equity = 1.0x; venue cap 0.5 must bind
    d = g.evaluate(**_kwargs(quantity=2.0, account=_account(leverage_cap=0.5)))
    assert not d.approved and d.reason_code == "max_leverage"


def test_total_exposure_cap_vetoes(gate):
    g, _, _ = gate
    # 20 ETH @3k = 60k existing + 2 BTC @50k = 100k new → 160% of equity > 100%
    d = g.evaluate(
        **_kwargs(
            quantity=2.0,
            account=_account(
                positions=(
                    BrokerPosition(asset_id="ETHUSD", quantity=20.0, avg_entry_price=3_000.0),
                )
            ),
        )
    )
    assert not d.approved and d.reason_code == "max_total_exposure"


def test_position_and_order_count_caps_veto_only_for_new_assets(gate):
    g, _, _ = gate
    held = (BrokerPosition(asset_id="BTCUSD", quantity=0.1, avg_entry_price=50_000.0),)
    # adding to an existing asset bypasses the position-count veto...
    d = g.evaluate(
        **_kwargs(account=_account(positions=held * 3)),  # 3 positions, same asset
    )
    assert d.approved
    # ...but a brand-new asset at the cap is blocked
    other = held[:-1] + (BrokerPosition(asset_id="ETHUSD", quantity=1.0, avg_entry_price=3_000.0),)
    d = g.evaluate(**_kwargs(account=_account(positions=other)))
    assert not d.approved and d.reason_code == "max_open_positions"


def test_open_order_count_cap_vetoes(gate):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(open_orders_count=6))
    assert not d.approved and d.reason_code == "max_open_orders"


def test_notional_caps_veto(gate):
    g, store, _ = gate

    small = HardRiskGate(
        limits=LiveRiskLimits(max_order_value=4_000.0),
        store=store,
        breaker=CircuitBreaker(store=store),
    )
    d = small.evaluate(**_kwargs())
    assert not d.approved and d.reason_code == "max_order_value"

    pos_cap = HardRiskGate(
        limits=LiveRiskLimits(max_position_notional=4_000.0),
        store=store,
        breaker=CircuitBreaker(store=store),
    )
    d = pos_cap.evaluate(**_kwargs())
    assert not d.approved and d.reason_code == "max_position_notional"


def test_missing_stop_level_vetoes_risky_entry(gate):
    g, _, _ = gate
    d = g.evaluate(**_kwargs(stop_distance_pct=None))
    assert not d.approved and d.reason_code == "missing_stop_level"
    d = g.evaluate(**_kwargs(stop_distance_pct=0.0))
    assert not d.approved and d.reason_code == "missing_stop_level"


def test_risk_per_trade_budget_veto(gate):
    """0.1 units @50k with a 2% stop risks 100; budget 0.05% of 100k = 50."""
    g, store, _ = gate
    tight = HardRiskGate(
        limits=LiveRiskLimits(max_risk_per_trade_pct=0.05),
        store=store,
        breaker=CircuitBreaker(store=store),
    )
    d = tight.evaluate(**_kwargs())
    assert not d.approved and d.reason_code == "risk_per_trade"


def test_internal_exception_fails_closed_as_gate_error(tmp_path):
    store = ExecutionStore(root=tmp_path / "s")
    gate = HardRiskGate(
        limits=LiveRiskLimits(),
        store=store,
        breaker=CircuitBreaker(store=store),
    )
    # NaN equity poisons arithmetic; the gate must still veto deterministically
    d = gate.evaluate(**_kwargs(account=_account(equity=float("nan"))))
    assert not d.approved
    assert d.reason_code in {"gate_error", "account_unhealthy", "max_leverage"}
