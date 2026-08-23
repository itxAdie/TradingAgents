"""Point-in-time market-data provider over a stored historical dataset.

Implements the *existing* Phase 1
:class:`~tradingagents.marketdata.provider.MarketDataProvider` protocol, so
the research engine consumes it exactly like the live Yahoo adapter. The
structural no-look-ahead guarantee: ``get_ohlcv`` only ever returns bars with
``timestamp <= clock.now()`` — future rows exist in memory but are
invisible to every consumer (ARCHITECTURE.md P2.4).
"""

from __future__ import annotations

import bisect
from datetime import datetime

from tradingagents.assets.registry import AssetSpec
from tradingagents.backtest.clock import SimulationClock
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries, Quote
from tradingagents.marketdata.timeframes import Timeframe


class ReplayMarketDataProvider:
    """Serves a validated historical dataset up to the simulated instant.

    A bar stamped ``T`` is treated as knowable **at its close**, i.e. from
    instant ``T`` onward (decision timestamps align with bar-close times).
    """

    name = "replay"

    def __init__(self, dataset: OhlcvSeries, clock: SimulationClock):
        if dataset.status is not DataStatus.HISTORICAL:
            raise ValueError(
                "replay datasets must be labelled HISTORICAL; got "
                f"{dataset.status.value}"
            )
        if not dataset.bars:
            raise ValueError("cannot replay an empty dataset")
        self.dataset = dataset
        self.clock = clock
        self._stamps = [b.timestamp for b in dataset.bars]
        self.calls = 0  # telemetry: number of get_ohlcv invocations

    # -- internal -------------------------------------------------------------

    def _visible_index(self) -> int:
        """Count of bars with timestamp <= now (bisect: O(log n))."""
        return bisect.bisect_right(self._stamps, self.clock.now())

    # -- MarketDataProvider protocol -------------------------------------------

    def get_ohlcv(
        self,
        asset: AssetSpec,
        timeframe: Timeframe,
        *,
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> OhlcvSeries:
        self.calls += 1
        if asset.asset_id != self.dataset.asset_id:
            raise NoMarketDataError(
                asset.yahoo_symbol, canonical=asset.asset_id,
                detail="asset not present in replay dataset",
            )
        if timeframe is not self.dataset.timeframe:
            raise ValueError(
                f"replay dataset is {self.dataset.timeframe.value}, "
                f"requested {timeframe.value}"
            )
        cutoff = self.clock.now()
        lo = bisect.bisect_left(self._stamps, start) if start is not None else 0
        hi = min(
            self._visible_index(),
            bisect.bisect_right(self._stamps, end) if end is not None else len(self._stamps),
        )
        bars: list[Bar] = self.dataset.bars[lo:hi]
        if limit is not None and len(bars) > limit:
            bars = bars[-limit:]
        if not bars:
            raise NoMarketDataError(
                asset.yahoo_symbol, canonical=asset.asset_id,
                detail=f"no bars at or before {cutoff.isoformat()}",
            )
        return OhlcvSeries(
            asset_id=self.dataset.asset_id,
            timeframe=self.dataset.timeframe,
            source=f"replay:{self.dataset.source}",
            status=DataStatus.HISTORICAL,
            bars=bars,
        )

    def get_quote(self, asset: AssetSpec) -> Quote | None:
        """Historical quotes don't exist; last visible close is not a quote."""
        return None


__all__ = ["ReplayMarketDataProvider"]
