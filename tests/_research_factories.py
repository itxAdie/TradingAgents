"""Shared factories for Phase 1 research-platform tests (all offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_bars(
    count: int,
    *,
    end: datetime | None = None,
    hours_step: int = 1,
    base_price: float = 100.0,
    drift_pct_per_bar: float = 0.0,
    zigzag_pct_per_bar: float = 0.0,
    volume: float | None = 1000.0,
) -> list[Bar]:
    """Deterministic bars ascending toward ``end`` (default: now).

    ``drift_pct_per_bar`` compounds one direction; ``zigzag_pct_per_bar``
    alternates +/- moves so close-to-close returns vary (needed for
    non-degenerate volatility).
    """
    end = end or utc_now()
    bars: list[Bar] = []
    price = base_price
    for i in range(count):
        ts = end - timedelta(hours=hours_step * (count - 1 - i))
        open_p = price
        move = drift_pct_per_bar + (zigzag_pct_per_bar if i % 2 == 0 else -zigzag_pct_per_bar)
        close_p = price * (1 + move / 100)
        bars.append(
            Bar(
                timestamp=ts,
                open=open_p,
                high=max(open_p, close_p) * 1.001,
                low=min(open_p, close_p) * 0.999,
                close=close_p,
                volume=volume,
            )
        )
        price = close_p
    return bars


def make_series(
    count: int,
    *,
    asset_id: str = "XAUUSD",
    timeframe: str = "1h",
    status: DataStatus = DataStatus.DELAYED,
    **bar_kwargs: Any,
) -> OhlcvSeries:
    return OhlcvSeries(
        asset_id=asset_id,
        timeframe=timeframe,
        source="test",
        status=status,
        bars=make_bars(count, **bar_kwargs),
    )


class FakeStructuredLLM:
    """Stand-in chat model for structured-output nodes.

    ``queue`` items are consumed in order by every ``invoke``; an item may be
    an exception (raised) or a schema instance (returned).
    """

    def __init__(self, queue: list[Any] | None = None):
        self.queue: list[Any] = list(queue or [])
        self.prompts: list[str] = []
        self.bound_schemas: list[type] = []

    def with_structured_output(self, schema: type):
        self.bound_schemas.append(schema)
        outer = self

        class _Bound:
            def invoke(self, prompt: str):
                outer.prompts.append(prompt)
                if not outer.queue:
                    raise AssertionError("FakeStructuredLLM queue exhausted")
                item = outer.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        return _Bound()

    def invoke(self, prompt: str):  # plain path must never be used in research
        raise AssertionError("research agents must use structured output only")


class FakeProvider:
    """Minimal MarketDataProvider returning canned series."""

    name = "fake"

    def __init__(self, series: OhlcvSeries | None = None, error: Exception | None = None):
        self.series = series
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def get_ohlcv(self, asset, timeframe, *, limit=None, start=None, end=None):
        self.calls.append((asset.asset_id, timeframe.value))
        if self.error is not None:
            raise self.error
        return self.series

    def get_quote(self, asset):
        return None
