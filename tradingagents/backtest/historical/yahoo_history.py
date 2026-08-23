"""Fetch and persist historical datasets (Yahoo Finance via Phase 1 stack).

Reuses :class:`~tradingagents.marketdata.yahoo_provider.YahooMarketDataProvider`
(same retry wrapper, symbol mapping, H4 resampling) and re-labels the result
as :attr:`DataStatus.HISTORICAL` with the fetch window recorded in metadata.

Known Yahoo retention limits are surfaced, not hidden: a requested range
older than ``Timeframe.max_history_days()`` is rejected up front instead of
returning silently truncated data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from tradingagents.assets.registry import AssetSpec
from tradingagents.backtest.historical.store import (
    DatasetMeta,
    HistoricalDataStore,
    build_meta,
)
from tradingagents.backtest.historical.validation import (
    ValidationReport,
    ensure_valid,
)
from tradingagents.marketdata.models import DataStatus, OhlcvSeries
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.marketdata.yahoo_provider import YahooMarketDataProvider

logger = logging.getLogger(__name__)


def _to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fetch_history(
    asset: AssetSpec,
    timeframe: Timeframe,
    *,
    start: datetime,
    end: datetime,
    provider: YahooMarketDataProvider | None = None,
) -> tuple[OhlcvSeries, ValidationReport]:
    """Download one (asset, timeframe, start, end) window and validate it."""
    start, end = _to_utc(start), _to_utc(end)
    if start >= end:
        raise ValueError("start must be before end")
    retention_days = timeframe.max_history_days()
    span_days = (end - start).total_seconds() / 86400
    if timeframe is not Timeframe.D1 and span_days > retention_days:
        raise ValueError(
            f"{timeframe.value} history beyond Yahoo retention (~{retention_days} days); "
            "narrow the requested window or use daily bars"
        )
    impl = provider or YahooMarketDataProvider()
    series = impl.get_ohlcv(
        asset, timeframe, start=start, end=end + timedelta(minutes=1)
    )
    # Honest label: anything we persist for replay is a historical record.
    series = series.model_copy(update={"status": DataStatus.HISTORICAL})
    report = ensure_valid(series)
    logger.info(
        "fetched %s @ %s: %s", asset.asset_id, timeframe.value, report.summary()
    )
    return series, report


def fetch_and_store(
    asset: AssetSpec,
    timeframe: Timeframe,
    *,
    start: datetime,
    end: datetime,
    store: HistoricalDataStore,
    provider: YahooMarketDataProvider | None = None,
) -> tuple[OhlcvSeries, DatasetMeta]:
    """Fetch, validate, persist; returns the dataset and its provenance."""
    series, _report = fetch_history(
        asset, timeframe, start=start, end=end, provider=provider
    )
    meta = build_meta(series, provider_symbol=asset.yahoo_symbol)
    path = store.save(series, meta)
    logger.info("stored dataset %s -> %s", meta.dataset_id, path.name)
    return series, meta


__all__ = ["fetch_and_store", "fetch_history"]
