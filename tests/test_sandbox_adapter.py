"""SandboxBrokerAdapter behaviour: a real deterministic state machine.

The scripted outcomes must reproduce every lifecycle event honestly —
including the timeout case, where the venue records the order BEFORE the
response is lost so UNKNOWN is truthful rather than fabricated.
"""

from __future__ import annotations

import pytest

from tests.broker_helpers import SteppingClock
from tradingagents.brokers.base import (
    BrokerError,
    BrokerOrderStatus,
    ConnectionStatus,
    ErrorClass,
)
from tradingagents.brokers.sandbox import SandboxBrokerAdapter, _ScriptedEvent


@pytest.fixture()
def adapter():
    a = SandboxBrokerAdapter(account_id="sbx-t", starting_cash=10_000.0, clock=SteppingClock())
    a.connect()
    return a


def _submit(a: SandboxBrokerAdapter, **kw):
    kw.setdefault("client_order_id", "TA-demo-sbx-t-ab12cd34-1")
    kw.setdefault("asset_id", "BTCUSD")
    kw.setdefault("side", "BUY")
    kw.setdefault("order_type", "MARKET")
    kw.setdefault("quantity", 0.1)
    return a.submit_order(**kw)


def test_starts_disconnected_until_connect():
    a = SandboxBrokerAdapter(clock=SteppingClock())
    assert a.health_check() is ConnectionStatus.DISCONNECTED
    assert a.connect() is ConnectionStatus.CONNECTED
    assert a.health_check() is ConnectionStatus.CONNECTED


def test_default_fill_moves_cash_and_position(adapter):
    outcome = _submit(adapter)
    assert outcome.unknown is False
    info = outcome.order
    assert info.status is BrokerOrderStatus.FILLED
    snap = adapter.get_account()
    fee = round(100.0 * 0.1 * 0.0001, 4)  # sandbox default fill price is 100.0
    assert snap.cash == pytest.approx(10_000.0 - 0.1 * 100.0 - fee)
    positions = {p.asset_id: p for p in adapter.get_positions()}
    assert positions["BTCUSD"].quantity == pytest.approx(0.1)


def test_scripted_price_is_honoured(adapter):
    adapter.script([_ScriptedEvent(kind="fill", price=123.5)])
    info = _submit(adapter).order
    assert info.avg_fill_price == pytest.approx(123.5)


def test_partial_fill_keeps_order_open(adapter):
    adapter.script([_ScriptedEvent(kind="partial", price=101.0, quantity=0.04)])
    info = _submit(adapter).order
    assert info.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert info.filled_quantity == pytest.approx(0.04)
    # remaining quantity still visible as an open order venue-side
    open_ids = {o.client_order_id for o in adapter.get_orders(open_only=True)}
    assert "TA-demo-sbx-t-ab12cd34-1" in open_ids


def test_reject_raises_non_retryable(adapter):
    adapter.script([_ScriptedEvent(kind="reject", code="margin", message="no margin")])
    with pytest.raises(BrokerError) as excinfo:
        _submit(adapter)
    assert excinfo.value.classification is ErrorClass.NON_RETRYABLE


def test_rate_limit_raises_retryable(adapter):
    adapter.script([_ScriptedEvent(kind="rate_limit")])
    with pytest.raises(BrokerError) as excinfo:
        _submit(adapter)
    assert excinfo.value.classification is ErrorClass.RETRYABLE


def test_timeout_records_order_before_losing_response(adapter):
    """The honesty property: UNKNOWN means 'may exist', not 'does not exist'."""
    adapter.script([_ScriptedEvent(kind="timeout")])
    outcome = _submit(adapter)
    assert outcome.unknown is True
    assert outcome.order is None
    found = adapter.find_order(client_order_id="TA-demo-sbx-t-ab12cd34-1")
    assert found is not None
    assert found.status is BrokerOrderStatus.ACKNOWLEDGED


def test_same_client_id_never_duplicates_an_order(adapter):
    first = _submit(adapter)
    second = _submit(adapter)  # identical client id
    assert second.order.broker_order_id == first.order.broker_order_id
    assert second.detail == "existing"


def test_malformed_preserves_broker_raw_without_crash(adapter):
    adapter.script([_ScriptedEvent(kind="malformed", message="\x00binary")])
    with pytest.raises(BrokerError) as excinfo:
        _submit(adapter)
    assert excinfo.value.classification is ErrorClass.NON_RETRYABLE
    assert "\x00binary" in (excinfo.value.broker_raw or "")


def test_disconnected_calls_fail_unknown_classified(adapter):
    adapter.force_status(ConnectionStatus.DISCONNECTED)
    with pytest.raises(BrokerError) as excinfo:
        adapter.get_account()
    assert excinfo.value.classification is ErrorClass.UNKNOWN


def test_injected_position_is_visible_for_reconciliation_drills(adapter):
    adapter.inject_position("XAUUSD", quantity=-2.0, avg_price=2400.0)
    positions = {p.asset_id: p for p in adapter.get_positions()}
    assert positions["XAUUSD"].quantity == pytest.approx(-2.0)


def test_dropped_order_disappears_from_book(adapter):
    _submit(adapter)
    adapter.drop_order("TA-demo-sbx-t-ab12cd34-1")
    assert adapter.find_order(client_order_id="TA-demo-sbx-t-ab12cd34-1") is None


def test_cancel_and_modify_lifecycle(adapter):
    adapter.script([_ScriptedEvent(kind="partial", price=100.0, quantity=0.01)])
    cid = "TA-demo-sbx-t-ab12cd34-1"
    _submit(adapter, client_order_id=cid, limit_price=100.0, order_type="LIMIT")
    modified = adapter.modify_order(client_order_id=cid, limit_price=101.5)
    assert modified.limit_price == pytest.approx(101.5)
    assert adapter.cancel_order(client_order_id=cid) is True
    assert adapter.find_order(client_order_id=cid).status is BrokerOrderStatus.CANCELLED
    assert adapter.cancel_order(client_order_id=cid) is False  # terminal already


def test_cancel_unknown_order_returns_false_not_error(adapter):
    assert adapter.cancel_order(client_order_id="TA-demo-x-x-9") is False


def test_call_log_records_operations(adapter):
    _submit(adapter)
    ops = [entry["op"] for entry in adapter.raw_call_log()]
    assert "submit_order" in ops
