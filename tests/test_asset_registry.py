"""Unit tests: asset registry (Phase 1)."""

from __future__ import annotations

import pytest

from tradingagents.assets.registry import (
    AssetClass,
    AssetSpec,
    UnknownAssetError,
    get_asset,
    list_assets,
    normalize_asset_id,
    register_asset,
)


@pytest.mark.unit
class TestCanonicalAssets:
    def test_gold_resolves(self):
        spec = get_asset("XAUUSD")
        assert spec.asset_id == "XAUUSD"
        assert spec.display_name == "XAU/USD"
        assert spec.yahoo_symbol == "GC=F"
        assert spec.quote_currency == "USD"

    def test_btc_resolves(self):
        spec = get_asset("BTCUSD")
        assert spec.asset_id == "BTCUSD"
        assert spec.yahoo_symbol == "BTC-USD"
        assert spec.asset_class is AssetClass.CRYPTO

    def test_aliases_and_spellings(self):
        assert get_asset("xau/usd").asset_id == "XAUUSD"
        assert get_asset("btc-usd").asset_id == "BTCUSD"
        assert get_asset("GOLD").asset_id == "XAUUSD"
        assert get_asset("bitcoin").asset_id == "BTCUSD"

    def test_unknown_asset_is_loud(self):
        with pytest.raises(UnknownAssetError):
            get_asset("NOPE")

    def test_macro_context_present(self):
        gold = get_asset("XAUUSD")
        assert any("interest" in f.lower() for f in gold.macro_context)
        btc = get_asset("BTCUSD")
        assert any("crypto" in f.lower() for f in btc.macro_context)


@pytest.mark.unit
class TestRegistryExtension:
    def test_register_new_asset(self):
        spec = AssetSpec(
            asset_id="EURUSD",
            display_name="EUR/USD",
            asset_class=AssetClass.FOREX,
            quote_currency="USD",
            provider_symbols={"yahoo": "EURUSD=X"},
        )
        register_asset(spec)
        assert get_asset("EURUSD") is spec
        ids = [a.asset_id for a in list_assets()]
        assert "EURUSD" in ids
        # cleanup so other tests see the pristine registry
        from tradingagents.assets import registry as reg
        del reg._REGISTRY["EURUSD"]

    def test_missing_provider_symbol_raises(self):
        spec = AssetSpec(
            asset_id="TEST",
            display_name="Test",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            provider_symbols={},
        )
        with pytest.raises(KeyError, match="Yahoo"):
            _ = spec.yahoo_symbol


@pytest.mark.unit
def test_normalize_asset_id_pure():
    assert normalize_asset_id(" xauusd ") == "XAUUSD"
    assert normalize_asset_id("XAU/USD+") == "XAUUSD"
