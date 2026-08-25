"""BrokerAdapter contract suite (P5 §10–§13).

The protocol is the only seam to a venue: minimum permission surface
(no withdrawal/transfer/funding methods at all), explicit error classes,
honest UNKNOWN outcomes. The sandbox adapter must satisfy all of it — any
future real adapter runs this same suite before registration.
"""

from __future__ import annotations

import inspect

import pytest

from tradingagents.brokers.base import (
    BrokerAccountSnapshot,
    BrokerAdapter,
    BrokerError,
    BrokerOrderInfo,
    BrokerOrderStatus,
    BrokerPosition,
    ErrorClass,
    SubmitOutcome,
)
from tradingagents.brokers.registry import (
    available_brokers,
    build_broker,
    register_broker,
)
from tradingagents.brokers.sandbox import SandboxBrokerAdapter

FORBIDDEN_METHODS = {
    "withdraw",
    "withdrawal",
    "transfer",
    "deposit",
    "fund",
    "funding",
    "create_account",
    "close_account",
    "set_leverage",
    "add_api_key",
    "reset_api_key",
}


def test_sandbox_satisfies_protocol():
    adapter = SandboxBrokerAdapter()
    assert isinstance(adapter, BrokerAdapter)


def test_adapter_surface_has_no_money_movement_methods():
    protocol_methods = {
        name
        for name, member in vars(BrokerAdapter).items()
        if callable(member) and not name.startswith("_")
    }
    assert protocol_methods, "protocol surface vanished"
    clashes = protocol_methods & FORBIDDEN_METHODS
    assert not clashes, f"forbidden methods on protocol: {sorted(clashes)}"

    adapter = SandboxBrokerAdapter()
    public = {name for name in dir(adapter) if not name.startswith("_")}
    clashes = {n for n in public if n.lower() in FORBIDDEN_METHODS}
    assert not clashes, f"forbidden methods on sandbox adapter: {sorted(clashes)}"


def test_error_classification_is_explicit_and_complete():
    for cls in ErrorClass:
        err = BrokerError(cls, f"code_{cls.value}", "message")
        assert err.classification is cls
        assert err.code.startswith("code_")
        assert "supersecret" not in str(err)


def test_submitoutcome_unknown_flag_defaults_false():
    outcome = SubmitOutcome(order=None)
    assert outcome.unknown is False


def test_broker_order_info_preserves_raw_status_and_pending_fees():
    info = BrokerOrderInfo(
        broker_order_id="B1",
        client_order_id="TA-demo-acc-abcdef12-1",
        asset_id="BTCUSD",
        side="BUY",
        order_type="MARKET",
        quantity=1.0,
        filled_quantity=0.0,
        avg_fill_price=None,
        status=BrokerOrderStatus.PENDING_SUBMIT,
        fees_reported=None,
        raw_status="WEIRD-VENUE-STATE",
    )
    assert info.raw_status == "WEIRD-VENUE-STATE"
    assert info.fees_reported is None  # pending fees are never invented


def test_account_snapshot_separates_venue_cap_from_system_cap():
    snap = BrokerAccountSnapshot(
        account_id="a",
        currency="USD",
        cash=1_000.0,
        equity=1_000.0,
        positions=(),
        open_orders=(),
        server_time=None,
        leverage_cap=20.0,
    )
    assert snap.leverage_cap == 20.0  # venue maximum only; system cap lives in limits


def test_position_carries_protective_order_flag():
    pos = BrokerPosition(asset_id="BTCUSD", quantity=1.0, avg_entry_price=1.0)
    assert pos.protective_orders_ok is False  # unverified until proven otherwise


# -- registry ------------------------------------------------------------------------


@pytest.fixture()
def clean_registry():
    """Snapshot the registry around tests that mutate it."""
    import tradingagents.brokers.registry as reg

    before = dict(reg._REGISTRY)
    yield
    reg._REGISTRY.clear()
    reg._REGISTRY.update(before)


def test_unknown_broker_fails_closed_without_substitution(clean_registry):
    with pytest.raises(ValueError, match="unknown broker"):
        build_broker("does-not-exist")


def test_registered_adapter_name_must_match(clean_registry):
    register_broker("liar", lambda **kw: SandboxBrokerAdapter())
    with pytest.raises(ValueError, match="mismatch"):
        build_broker("liar")


def test_register_broker_roundtrip(clean_registry):
    class Named(SandboxBrokerAdapter):
        name = "named"

    register_broker("named", lambda **kw: Named())
    assert "named" in available_brokers()
    assert isinstance(build_broker("named"), BrokerAdapter)


def test_sandbox_is_the_only_registered_adapter_by_default():
    # Phase 5 scope decision: real venues arrive only after the contract suite.
    assert available_brokers() == ["sandbox"]


def test_factory_signatures_accept_keyword_configuration():
    adapter = build_broker(
        "sandbox", account_id="acc-1", base_currency="USD", starting_cash=5_000.0
    )
    adapter.connect()
    assert adapter.get_account().account_id == "acc-1"


def test_protocol_docstring_documents_permission_surface():
    doc = inspect.getdoc(BrokerAdapter) or ""
    assert "contract" in doc.lower()
