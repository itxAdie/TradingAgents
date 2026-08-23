"""Normalized internal market-data models.

Every datum crossing the research system carries its nature with it:
``source``, ``timeframe``, and a :class:`DataStatus` from the fixed set
real-time / delayed / historical / cached / simulated. Timestamps are always
timezone-aware UTC — naive datetimes are rejected at validation time so
timezone bugs cannot slip through silently. A series may not mix bars of
different timestamps' granularity or different statuses; validators enforce
sorted, de-duplicated bars of uniform shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.marketdata.timeframes import Timeframe


class DataStatus(str, Enum):
    """Explicit data-nature labels (PROJECT_RULES §4)."""

    REALTIME = "real_time"
    DELAYED = "delayed"
    HISTORICAL = "historical"
    CACHED = "cached"
    SIMULATED = "simulated"


class DataFreshness(str, Enum):
    """Coarse freshness verdict computed by the engine, not the provider."""

    FRESH = "fresh"  # newest bar within staleness window
    STALE = "stale"  # older than the timeframe's staleness window


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(
            f"timestamp must be timezone-aware, got naive {value!r}; "
            "attach tzinfo (timestamps are normalized to UTC)"
        )
    return value.astimezone(timezone.utc)


class Bar(BaseModel):
    """One OHLCV row. ``timestamp`` is timezone-aware UTC."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float | None = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @model_validator(mode="after")
    def _high_low_consistent(self) -> Self:
        if self.high < max(self.open, self.close) - 1e-12:
            raise ValueError(f"high {self.high} below body max (open/close)")
        if self.low > min(self.open, self.close) + 1e-12:
            raise ValueError(f"low {self.low} above body min (open/close)")
        if self.high < self.low:
            raise ValueError(f"high {self.high} < low {self.low}")
        return self

    def to_row(self) -> dict[str, float]:
        row = {
            "Date": self.timestamp,
            "Open": self.open,
            "High": self.high,
            "Low": self.low,
            "Close": self.close,
        }
        if self.volume is not None:
            row["Volume"] = self.volume
        return row


class OhlcvSeries(BaseModel):
    """Uniform, ordered OHLCV bars for one asset/timeframe."""

    asset_id: str
    timeframe: Timeframe
    source: str  # e.g. "yahoo"
    status: DataStatus
    bars: list[Bar] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_unique(self) -> Self:
        stamps = [b.timestamp for b in self.bars]
        if any(
            later <= earlier
            for earlier, later in zip(stamps, stamps[1:], strict=False)
        ):
            raise ValueError("bars must be strictly ascending by timestamp")
        if len(set(stamps)) != len(stamps):
            raise ValueError("duplicate bar timestamps")
        # Uniform volume presence: either every bar has volume or none does,
        # so indicator code never branches on per-row availability.
        has_volume = [b.volume is not None for b in self.bars]
        if any(has_volume) and not all(has_volume):
            raise ValueError("volume must be present on all bars or none")
        return self

    @property
    def latest_timestamp(self) -> datetime | None:
        return self.bars[-1].timestamp if self.bars else None

    @property
    def latest_close(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.bars)


class Quote(BaseModel):
    """A point-in-time quote snapshot."""

    asset_id: str
    timestamp: datetime
    last: float | None = Field(default=None, gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)
    source: str
    status: DataStatus

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc(v)


def classify_status(asset_class: str, newest_bar_age_hours: float | None) -> DataStatus:
    """Deterministic data-status label for freshly fetched Yahoo data.

    Non-crypto exchange data on Yahoo is delayed at best (~15 min), so it is
    never labelled REALTIME; crypto trades continuously and a current bar is
    genuinely real-time. Old data becomes HISTORICAL regardless of venue.
    """
    if newest_bar_age_hours is None:
        return DataStatus.HISTORICAL
    if newest_bar_age_hours > 24 * 2:  # beyond ~2 days old -> historical record
        return DataStatus.HISTORICAL
    if asset_class == "crypto":
        return DataStatus.REALTIME
    return DataStatus.DELAYED
