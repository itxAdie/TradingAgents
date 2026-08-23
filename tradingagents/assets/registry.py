"""Centralized asset registry.

Canonical internal identifiers (`XAUUSD`, `BTCUSD`, ...) are the single source
of truth for every downstream component. Provider-specific symbols (Yahoo's
``GC=F`` / ``BTC-USD``) live here as mapping rows — never scattered through
agents or call sites. Adding EUR/USD, ETH/USD, NASDAQ, or individual stocks
later means appending a registry entry (or calling :func:`register_asset`),
not touching the research engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AssetClass(str, Enum):
    METAL = "metal"
    CRYPTO = "crypto"
    FOREX = "forex"
    INDEX = "index"
    EQUITY = "equity"
    ENERGY = "energy"


@dataclass(frozen=True)
class AssetSpec:
    """Immutable description of one tradable instrument."""

    asset_id: str  # canonical internal ID, e.g. "XAUUSD"
    display_name: str  # human-facing name, e.g. "XAU/USD"
    asset_class: AssetClass
    quote_currency: str  # e.g. "USD"
    provider_symbols: dict[str, str]  # {"yahoo": "GC=F"}
    # Deterministic hints telling the macro analyst which factors matter for
    # this asset. Data itself must still come from real sources only.
    macro_context: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    @property
    def yahoo_symbol(self) -> str:
        symbol = self.provider_symbols.get("yahoo")
        if not symbol:
            raise KeyError(f"No Yahoo symbol registered for {self.asset_id}")
        return symbol

    def provider_symbol(self, provider: str) -> str:
        try:
            return self.provider_symbols[provider]
        except KeyError as exc:
            raise KeyError(
                f"Asset {self.asset_id} has no symbol registered for provider {provider!r}; "
                f"known providers: {sorted(self.provider_symbols)}"
            ) from exc


_GOLD_MACRO = (
    "USD strength (DXY)",
    "Federal Reserve policy and interest-rate expectations",
    "US Treasury yields (real yields especially)",
    "inflation data (CPI/PCE)",
    "central-bank gold demand",
    "geopolitical risk",
)

_CRYPTO_MACRO = (
    "crypto-market risk sentiment (dominance, funding, liquidations)",
    "spot-ETF flow context where reported by fetched news",
    "regulatory/news events actually present in the fetched headlines",
)

_REGISTRY: dict[str, AssetSpec] = {
    spec.asset_id: spec
    for spec in (
        AssetSpec(
            asset_id="XAUUSD",
            display_name="XAU/USD",
            asset_class=AssetClass.METAL,
            quote_currency="USD",
            provider_symbols={"yahoo": "GC=F"},
            macro_context=_GOLD_MACRO,
            description=(
                "Spot-gold identifier. Yahoo has no spot pair; prices come from "
                "the COMEX front-month future GC=F, so quotes are delayed and "
                "session hours differ from 24h spot."
            ),
        ),
        AssetSpec(
            asset_id="BTCUSD",
            display_name="BTC/USD",
            asset_class=AssetClass.CRYPTO,
            quote_currency="USD",
            provider_symbols={"yahoo": "BTC-USD"},
            macro_context=_CRYPTO_MACRO,
            description=(
                "Bitcoin against US dollar; trades continuously on crypto venues."
            ),
        ),
    )
}


class UnknownAssetError(KeyError):
    """Raised when an asset id is not present in the registry."""


def normalize_asset_id(raw: str) -> str:
    """Map common spellings to canonical ids (purely syntactic)."""
    if not isinstance(raw, str):
        raise UnknownAssetError(f"Asset id must be a string, got {type(raw)!r}")
    compact = raw.strip().upper().replace("/", "").replace("-", "").rstrip("+")
    aliases = {
        "GOLD": "XAUUSD",
        "XAU": "XAUUSD",
        "BTC": "BTCUSD",
        "BITCOIN": "BTCUSD",
        "XBTUSD": "BTCUSD",
    }
    return aliases.get(compact, compact)


def register_asset(spec: AssetSpec) -> None:
    """Add or replace a registry entry (extension point for later phases)."""
    _REGISTRY[spec.asset_id] = spec


def get_asset(asset_id: str) -> AssetSpec:
    """Return the :class:`AssetSpec` for a canonical id (or common alias).

    Raises:
        UnknownAssetError: if the id is not in the registry.
    """
    key = normalize_asset_id(asset_id)
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise UnknownAssetError(
            f"Unknown asset id {asset_id!r}. Registered assets: {sorted(_REGISTRY)}"
        ) from exc


def list_assets() -> tuple[AssetSpec, ...]:
    """All registered assets, ordered by asset id."""
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))
