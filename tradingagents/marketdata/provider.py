"""Market-data provider abstraction.

The research system depends on this interface only — never on a concrete
vendor. First implementation is :class:`tradingagents.marketdata.yahoo_provider.YahooMarketDataProvider`
(wrapping the yfinance access the repo already uses); additional providers
(Alpha Vantage, broker feeds, ...) implement the same two methods.

Providers raise the existing vendor-error taxonomy
(:mod:`tradingagents.dataflows.errors`) so routing/failure semantics stay
uniform across the codebase.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tradingagents.assets.registry import AssetSpec
from tradingagents.marketdata.models import OhlcvSeries, Quote
from tradingagents.marketdata.timeframes import Timeframe


@runtime_checkable
class MarketDataProvider(Protocol):
    """Contract every market-data backend must satisfy."""

    name: str

    def get_ohlcv(
        self,
        asset: AssetSpec,
        timeframe: Timeframe,
        *,
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> OhlcvSeries:
        """Return bars for ``asset`` at ``timeframe`` (ascending order).

        ``limit`` caps the number of returned bars (most recent kept).
        ``start``/``end`` bound the window (UTC-aware or naive-UTC); they are
        mutually compatible — when both are given both apply. Implementations
        must label the series with source and DataStatus and must never mix
        timeframes.
        """
        ...

    def get_quote(self, asset: AssetSpec) -> Quote | None:
        """Latest quote snapshot, or ``None`` when unavailable."""
        ...
