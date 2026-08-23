"""Yahoo Finance market-data provider.

Adapter that presents the repo's existing yfinance access behind the
:class:`~tradingagents.marketdata.provider.MarketDataProvider` interface.
Reuses the established conventions of this codebase:

- symbols come from the :mod:`tradingagents.assets` registry (already mapped
  through ``dataflows.symbol_utils.normalize_symbol`` rules),
- failures raise :class:`~tradingagents.dataflows.errors.NoMarketDataError`
  when a symbol yields no usable rows (same sentinel semantics as the vendor
  router),
- data-status labels are honest about Yahoo's nature: exchange-traded
  instruments are DELAYED at best (never REALTIME), crypto is REALTIME while
  fresh, anything old is HISTORICAL.

Yahoo has no native 4h interval, so H4 series are aggregated deterministically
from hourly bars anchored at 00:00 UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from tradingagents.assets.registry import AssetSpec
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.stockstats_utils import yf_retry
from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries, Quote, classify_status
from tradingagents.marketdata.timeframes import Timeframe

logger = logging.getLogger(__name__)

# Calendar-time multiplier applied to the requested bar span so weekends,
# holidays, and venue closures don't truncate the requested bar count.
_CALENDAR_BUFFER = 3


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fetch_frame(
    yahoo_symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Download raw OHLCV via yfinance with retry-on-rate-limit."""
    ticker = yf.Ticker(yahoo_symbol)
    frame = yf_retry(
        lambda: ticker.history(
            interval=timeframe.yfinance_interval,
            start=start,
            end=end,
            auto_adjust=False,
        )
    )
    if frame is None or frame.empty:
        raise NoMarketDataError(
            yahoo_symbol, detail=f"no {timeframe.value} rows in window"
        )
    return frame


def _resample_to_h4(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate an hourly frame into 4h bars anchored at 00:00 UTC."""
    agg = frame.resample(
        "4h", label="left", closed="left", origin="start_day"
    ).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    })
    return agg.dropna(subset=["Open", "High", "Low", "Close"])


def _frame_to_bars(frame: pd.DataFrame) -> list[Bar]:
    bars: list[Bar] = []
    for stamp, row in frame.iterrows():
        ts = pd.Timestamp(stamp)
        ts = (
            ts.tz_localize(timezone.utc) if ts.tzinfo is None
            else ts.tz_convert(timezone.utc)
        )
        volume = row.get("Volume")
        bars.append(
            Bar(
                timestamp=ts.to_pydatetime(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(volume) if pd.notna(volume) else None,
            )
        )
    return bars


class YahooMarketDataProvider:
    """Concrete :class:`MarketDataProvider` backed by yfinance."""

    name = "yahoo"

    def get_ohlcv(
        self,
        asset: AssetSpec,
        timeframe: Timeframe,
        *,
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> OhlcvSeries:
        now = datetime.now(timezone.utc)
        start = _to_utc(start)
        end = _to_utc(end)

        if start is None:
            span_minutes = timeframe.minutes * max(1, int(limit or 300))
            span = min(
                timedelta(minutes=span_minutes * _CALENDAR_BUFFER),
                timedelta(days=timeframe.max_history_days()),
            )
            start = now - span
        if end is None:
            end = now + timedelta(minutes=5)

        yahoo_symbol = asset.yahoo_symbol
        logger.debug(
            "yahoo get_ohlcv asset=%s tf=%s symbol=%s window=%s..%s",
            asset.asset_id, timeframe.value, yahoo_symbol, start.isoformat(),
            end.isoformat(),
        )

        frame = _fetch_frame(yahoo_symbol, timeframe, start, end)

        # Look-ahead safety: drop anything beyond the requested end.
        index = pd.DatetimeIndex(frame.index).tz_convert(timezone.utc)
        frame = frame[index <= pd.Timestamp(end)]
        if frame.empty:
            raise NoMarketDataError(
                asset.asset_id, canonical=yahoo_symbol, detail="window empty after filter"
            )

        if timeframe.needs_resampling:
            frame = _resample_to_h4(frame)
            if frame.empty:
                raise NoMarketDataError(
                    asset.asset_id,
                    canonical=yahoo_symbol,
                    detail="resampling produced no complete 4h bars",
                )

        if limit is not None and len(frame) > limit:
            frame = frame.tail(limit)

        bars = _frame_to_bars(frame)
        newest_age_hours = (
            (now - bars[-1].timestamp).total_seconds() / 3600 if bars else None
        )
        status = classify_status(asset.asset_class.value, newest_age_hours)

        series = OhlcvSeries(
            asset_id=asset.asset_id,
            timeframe=timeframe,
            source=self.name,
            status=status,
            bars=bars,
        )
        logger.info(
            "Fetched %d %s bars for %s from %s (status=%s, latest=%s)",
            len(bars), timeframe.value, asset.asset_id, self.name, status.value,
            bars[-1].timestamp.isoformat() if bars else "n/a",
        )
        return series

    def get_quote(self, asset: AssetSpec) -> Quote | None:
        """Best-effort last trade price via fast_info; ``None`` on failure.

        Quote endpoints on Yahoo are undocumented and flaky; callers must
        treat ``None`` as "no quote available" rather than retry forever.
        """
        try:
            info = yf.Ticker(asset.yahoo_symbol).fast_info
            last = info.get("last_price")
        except Exception as exc:  # noqa: BLE001 — quote is best-effort by design
            logger.warning("quote unavailable for %s: %s", asset.asset_id, exc)
            return None
        if not last:
            return None
        return Quote(
            asset_id=asset.asset_id,
            timestamp=datetime.now(timezone.utc),
            last=float(last),
            source=self.name,
            status=(
                DataStatus.REALTIME
                if asset.asset_class.value == "crypto"
                else DataStatus.DELAYED
            ),
        )
