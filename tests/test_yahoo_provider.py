"""Unit tests: Yahoo provider adapter (yfinance fully mocked, offline)."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from tradingagents.assets.registry import get_asset
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.marketdata import yahoo_provider as yp
from tradingagents.marketdata.models import DataStatus
from tradingagents.marketdata.timeframes import Timeframe


def _frame(stamps, closes):
    idx = pd.DatetimeIndex(pd.to_datetime(stamps))
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return pd.DataFrame(
        {
            "Open": [c - 0.5 for c in closes],
            "High": [c + 1.0 for c in closes],
            "Low": [c - 1.0 for c in closes],
            "Close": closes,
            "Volume": [10.0] * len(closes),
        },
        index=idx.rename("Datetime"),
    )


class _FakeTicker:
    def __init__(self, frame=None, error=None):
        self._frame = frame
        self._error = error

    def history(self, **kwargs):
        if self._error is not None:
            raise self._error
        assert kwargs.get("interval") in {"15m", "60m", "1d"}
        return self._frame


@pytest.fixture
def gold():
    return get_asset("XAUUSD")


@pytest.mark.unit
def test_daily_bars_labeled_delayed(gold, monkeypatch):
    now = datetime.now(timezone.utc)
    stamps = [(now - pd.Timedelta(days=d)).isoformat() for d in (2, 1)]
    fake = _FakeTicker(_frame(stamps, [2400.0, 2410.0]))
    monkeypatch.setattr(yp.yf, "Ticker", lambda symbol: fake)
    series = yp.YahooMarketDataProvider().get_ohlcv(gold, Timeframe.D1)
    assert len(series) == 2
    assert series.source == "yahoo"
    # Fresh-ish exchange data must never claim REALTIME on Yahoo.
    assert series.status == DataStatus.DELAYED
    assert series.bars[-1].close == 2410.0
    assert series.bars[0].timestamp.tzinfo is not None


@pytest.mark.unit
def test_h4_resampling_from_hourly(monkeypatch):
    stamps = [
        "2026-08-21 00:00", "2026-08-21 01:00", "2026-08-21 02:00", "2026-08-21 03:00",
        "2026-08-21 04:00", "2026-08-21 05:00",
    ]
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    monkeypatch.setattr(
        yp.yf, "Ticker", lambda symbol: _FakeTicker(_frame(stamps, closes))
    )
    asset = get_asset("BTCUSD")
    series = yp.YahooMarketDataProvider().get_ohlcv(asset, Timeframe.H4)
    # Two complete 4h buckets anchored at 00:00 UTC.
    assert len(series) == 2
    first = series.bars[0]
    assert (first.timestamp.hour, first.timestamp.minute) == (0, 0)
    # Fake frame builds Open = close - 0.5, so the bucket open is 99.5.
    assert first.open == pytest.approx(99.5)
    assert first.close == 103.0
    assert first.high == 104.0 and first.low == 99.0
    assert first.volume == 40.0


@pytest.mark.unit
def test_empty_result_raises_no_market_data(gold, monkeypatch):
    class EmptyTicker:
        def history(self, **kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(yp.yf, "Ticker", lambda symbol: EmptyTicker())
    with pytest.raises(NoMarketDataError):
        yp.YahooMarketDataProvider().get_ohlcv(gold, Timeframe.H1)


@pytest.mark.unit
def test_lookahead_rows_dropped(gold, monkeypatch):
    now = datetime.now(timezone.utc)
    future = now + pd.Timedelta(hours=5)
    stamps = [now - pd.Timedelta(hours=3), now - pd.Timedelta(hours=1), future]
    frame = _frame([s.isoformat() for s in stamps], [100.0, 101.0, 999.0])
    monkeypatch.setattr(yp.yf, "Ticker", lambda symbol: _FakeTicker(frame))
    series = yp.YahooMarketDataProvider().get_ohlcv(gold, Timeframe.H1, end=now)
    assert all(b.timestamp <= now for b in series.bars)


@pytest.mark.unit
def test_limit_keeps_most_recent(gold, monkeypatch):
    now = datetime.now(timezone.utc)
    stamps = [(now - pd.Timedelta(days=10 - i)).isoformat() for i in range(10)]
    closes = [2400.0 + i for i in range(10)]
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker(_frame(stamps, closes)))
    series = yp.YahooMarketDataProvider().get_ohlcv(gold, Timeframe.D1, limit=3)
    assert len(series) == 3
    assert series.bars[-1].close == closes[-1]


@pytest.mark.unit
def test_provider_satisfies_protocol():
    from tradingagents.marketdata.provider import MarketDataProvider

    assert isinstance(yp.YahooMarketDataProvider(), MarketDataProvider)
