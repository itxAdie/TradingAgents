"""Phase 2 — ReplayMarketDataProvider point-in-time guarantees."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.assets.registry import get_asset
from tradingagents.backtest.clock import SimulationClock
from tradingagents.backtest.historical.replay_provider import ReplayMarketDataProvider
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.marketdata.models import DataStatus
from tradingagents.marketdata.timeframes import Timeframe

UTC = timezone.utc
ASSET = get_asset("XAUUSD")


def _dataset(count: int = 10):
    from tests._research_factories import make_bars
    from tradingagents.marketdata.models import OhlcvSeries

    bars = make_bars(
        count,
        end=datetime(2025, 5, 1, 12, tzinfo=UTC),
        hours_step=1,
        drift_pct_per_bar=0.05,
    )
    return OhlcvSeries(
        asset_id="XAUUSD", timeframe=Timeframe.H1,
        source="yahoo", status=DataStatus.HISTORICAL, bars=bars,
    )


def test_rejects_non_historical_status() -> None:
    ds = _dataset()
    live = ds.model_copy(update={"status": DataStatus.REALTIME})
    clock = SimulationClock(ds.bars[0].timestamp)
    with pytest.raises(ValueError, match="HISTORICAL"):
        ReplayMarketDataProvider(live, clock)


def test_rejects_empty_dataset() -> None:
    from tradingagents.marketdata.models import OhlcvSeries

    empty = OhlcvSeries(
        asset_id="XAUUSD", timeframe=Timeframe.H1,
        source="x", status=DataStatus.HISTORICAL, bars=[],
    )
    clock = SimulationClock(datetime(2025, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="empty"):
        ReplayMarketDataProvider(empty, clock)


def test_future_bars_are_invisible_before_their_close() -> None:
    ds = _dataset(count=10)
    # Clock at bar 3's close: exactly 4 bars visible (0..3).
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[3].timestamp))
    series = provider.get_ohlcv(ASSET, Timeframe.H1)
    assert len(series.bars) == 4
    assert series.bars[-1].timestamp == ds.bars[3].timestamp


def test_bar_becomes_visible_exactly_at_its_own_timestamp() -> None:
    ds = _dataset(count=6)
    stamp = ds.bars[4].timestamp
    just_before = ReplayMarketDataProvider(
        ds, SimulationClock(stamp - timedelta(microseconds=1))
    )
    assert len(just_before.get_ohlcv(ASSET, Timeframe.H1).bars) == 4

    at_close = ReplayMarketDataProvider(ds, SimulationClock(stamp))
    assert len(at_close.get_ohlcv(ASSET, Timeframe.H1).bars) == 5


def test_no_market_data_error_when_clock_before_first_bar() -> None:
    ds = _dataset(count=5)
    early = SimulationClock(ds.bars[0].timestamp - timedelta(hours=1))
    provider = ReplayMarketDataProvider(ds, early)
    with pytest.raises(NoMarketDataError):
        provider.get_ohlcv(ASSET, Timeframe.H1)


def test_wrong_asset_rejected_with_domain_error() -> None:
    ds = _dataset(count=5)
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[-1].timestamp))
    other = get_asset("BTCUSD")
    with pytest.raises(NoMarketDataError):
        provider.get_ohlcv(other, Timeframe.H1)


def test_wrong_timeframe_rejected() -> None:
    ds = _dataset(count=5)
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[-1].timestamp))
    with pytest.raises(ValueError, match="replay dataset"):
        provider.get_ohlcv(ASSET, Timeframe.D1)


def test_start_end_and_limit_slicing_respect_cutoff() -> None:
    ds = _dataset(count=12)
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[7].timestamp))

    windowed = provider.get_ohlcv(
        ASSET, Timeframe.H1, start=ds.bars[2].timestamp, end=ds.bars[9].timestamp,
    )
    # end beyond the cutoff is clamped to visible data.
    stamps = [b.timestamp for b in windowed.bars]
    assert stamps[0] == ds.bars[2].timestamp
    assert stamps[-1] == ds.bars[7].timestamp
    assert len(windowed.bars) == 6

    limited = provider.get_ohlcv(ASSET, Timeframe.H1, limit=3)
    assert [b.timestamp for b in limited.bars] == [
        ds.bars[i].timestamp for i in (5, 6, 7)
    ]


def test_get_quote_is_always_none() -> None:
    ds = _dataset(count=3)
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[-1].timestamp))
    assert provider.get_quote(ASSET) is None


def test_calls_telemetry_counts_invocations() -> None:
    ds = _dataset(count=3)
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[-1].timestamp))
    assert provider.calls == 0
    provider.get_ohlcv(ASSET, Timeframe.H1)
    provider.get_ohlcv(ASSET, Timeframe.H1)
    assert provider.calls == 2
