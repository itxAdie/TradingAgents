"""Unit tests: normalized market-data models."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from tests._research_factories import make_bars, make_series, utc_now
from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries, classify_status


def _series(bars: list[Bar], asset_id: str = "XAUUSD") -> OhlcvSeries:
    """Construct directly so validators actually run."""
    return OhlcvSeries(
        asset_id=asset_id, timeframe="1h", source="test",
        status=DataStatus.DELAYED, bars=bars,
    )


@pytest.mark.unit
class TestBar:
    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            Bar(timestamp=datetime(2026, 1, 1), open=1, high=2, low=0.5, close=1.5)

    def test_timestamps_normalized_to_utc(self):
        bar = make_bars(1)[0]
        assert bar.timestamp.tzinfo is not None
        assert bar.timestamp.utcoffset().total_seconds() == 0

    def test_inconsistent_ohlc_rejected(self):
        now = utc_now()
        with pytest.raises(ValidationError):  # high below body max
            Bar(timestamp=now, open=10, high=9, low=8, close=9.5)
        with pytest.raises(ValidationError):  # low above body min
            Bar(timestamp=now, open=10, high=11, low=10.5, close=10.8)
        with pytest.raises(ValidationError):  # high < low
            Bar(timestamp=now, open=10, high=12, low=13, close=11)


@pytest.mark.unit
class TestOhlcvSeries:
    def test_accepts_uniform_series(self):
        series = make_series(5)
        assert len(series) == 5
        assert series.latest_close == series.bars[-1].close

    def test_unsorted_bars_rejected(self):
        bars = make_bars(4)
        with pytest.raises(ValidationError, match="ascending"):
            _series([bars[3], bars[1], bars[2], bars[0]])

    def test_duplicate_timestamps_rejected(self):
        bars = make_bars(3)
        bars.append(bars[-1].model_copy())
        with pytest.raises(ValidationError):
            _series(bars)

    def test_mixed_volume_presence_rejected(self):
        bars = make_bars(3)
        no_vol = bars[1].model_copy(update={"volume": None})
        with pytest.raises(ValidationError, match="volume"):
            _series([bars[0], no_vol, bars[2]])

    def test_volumeless_series_allowed(self):
        series = make_series(3, volume=None)
        assert all(b.volume is None for b in series.bars)

    def test_empty_series_valid_but_flagged_by_engine(self):
        series = make_series(0)
        assert series.latest_timestamp is None


@pytest.mark.unit
class TestClassifyStatus:
    def test_crypto_fresh_is_realtime(self):
        assert classify_status("crypto", 0.5) == DataStatus.REALTIME

    def test_exchange_data_never_realtime(self):
        # Gold futures quotes are delayed at best on Yahoo.
        assert classify_status("metal", 0.5) == DataStatus.DELAYED

    def test_old_data_is_historical(self):
        assert classify_status("crypto", 72.0) == DataStatus.HISTORICAL
        assert classify_status("crypto", None) == DataStatus.HISTORICAL
