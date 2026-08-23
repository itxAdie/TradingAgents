"""Historical dataset storage.

One JSON file per dataset under ``<data_cache_dir>/historical/``:

    {ASSET}_{TIMEFRAME}_{START}_{END}_{ID8}.json

- ``START``/``END`` are ``YYYYMMDDTHHMMSSZ`` (UTC) of the first/last bar.
- ``ID8`` is the first 8 hex chars of the SHA-256 over the canonical
  identity string ``source|asset_id|timeframe|first_bar_iso|last_bar_iso``
  — i.e. the id identifies the requested window, not the bytes.
- The file body holds a metadata header plus the bars, and a separate
  ``content_sha256`` over the serialized bar rows so any tampering/truncation
  is detected at load time (integrity, not security).

JSON (stdlib) is deliberate: zero new dependencies, human-inspectable, and
the :class:`HistoricalDataStore` protocol keeps the door open for Parquet /
DuckDB / Postgres implementations later without touching the backtester
(ARCHITECTURE.md P2.3). All timestamps inside are timezone-aware ISO-8601
strings normalized to UTC — the canonical internal timezone for the whole
system.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from tradingagents.backtest.historical.validation import HistoricalDataError
from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries
from tradingagents.marketdata.timeframes import Timeframe


class DatasetMeta(BaseModel):
    """Provenance record embedded in every stored dataset and every report."""

    dataset_id: str
    content_sha256: str
    asset_id: str
    timeframe: Timeframe
    source: str
    provider_symbol: str
    first_bar_timestamp: datetime
    last_bar_timestamp: datetime
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    bar_count: int
    timezone: str = "UTC"  # canonical internal timezone (documented constant)
    gaps: list[str] = Field(default_factory=list)  # flagged, never interpolated
    notes: str = ""


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def compute_dataset_id(
    source: str, asset_id: str, timeframe: Timeframe,
    first_bar: datetime, last_bar: datetime,
) -> str:
    """Stable identity of a dataset window (independent of fetch time)."""
    identity = "|".join((
        source, asset_id, timeframe.value,
        first_bar.astimezone(timezone.utc).isoformat(),
        last_bar.astimezone(timezone.utc).isoformat(),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]


def bars_content_hash(bars: list[Bar]) -> str:
    sha = hashlib.sha256()
    for bar in bars:
        sha.update(
            f"{bar.timestamp.astimezone(timezone.utc).isoformat()},"
            f"{bar.open!r},{bar.high!r},{bar.low!r},{bar.close!r},{bar.volume!r}\n".encode()
        )
    return sha.hexdigest()


@runtime_checkable
class HistoricalDataStore(Protocol):
    """Contract for historical storage backends."""

    def save(self, series: OhlcvSeries, meta: DatasetMeta) -> Path: ...
    def load(self, asset_id: str, timeframe: Timeframe) -> tuple[OhlcvSeries, DatasetMeta]: ...
    def list_datasets(self) -> list[DatasetMeta]: ...


class JsonDataStore:
    """File-per-dataset JSON store (default Phase 2 implementation)."""

    format_version = 1

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- internals ------------------------------------------------------------

    @staticmethod
    def filename(meta: DatasetMeta) -> str:
        return (
            f"{meta.asset_id}_{meta.timeframe.value}_"
            f"{_stamp(meta.first_bar_timestamp)}_{_stamp(meta.last_bar_timestamp)}_"
            f"{meta.dataset_id}.json"
        )

    def _path_for(self, meta: DatasetMeta) -> Path:
        return self.root / self.filename(meta)

    # -- API -------------------------------------------------------------------

    def save(self, series: OhlcvSeries, meta: DatasetMeta) -> Path:
        payload = {
            "format_version": self.format_version,
            "meta": json.loads(meta.model_dump_json()),
            "bars": [json.loads(bar.model_dump_json()) for bar in series.bars],
        }
        path = self._path_for(meta)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(
        self, asset_id: str, timeframe: Timeframe
    ) -> tuple[OhlcvSeries, DatasetMeta]:
        candidates = sorted(self.root.glob(f"{asset_id}_{timeframe.value}_*.json"))
        if not candidates:
            raise FileNotFoundError(
                f"no historical dataset for {asset_id} @ {timeframe.value} "
                f"in {self.root}"
            )
        path = candidates[-1]  # newest window wins on overlap
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            meta = DatasetMeta(**payload["meta"])
            bars = [Bar(**row) for row in payload["bars"]]
        except HistoricalDataError:
            raise
        except Exception as exc:  # noqa: BLE001 - any unreadable payload is corrupt
            raise HistoricalDataError(
                f"dataset {path.name} is unreadable or corrupt: {exc}"
            ) from exc
        if bars_content_hash(bars) != meta.content_sha256:
            raise HistoricalDataError(
                f"dataset integrity check failed: {path.name}"
            )
        series = OhlcvSeries(
            asset_id=meta.asset_id,
            timeframe=meta.timeframe,
            source=meta.source,
            status=DataStatus.HISTORICAL,
            bars=bars,
        )
        return series, meta

    def list_datasets(self) -> list[DatasetMeta]:
        out: list[DatasetMeta] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                out.append(DatasetMeta(**payload["meta"]))
            except Exception:  # noqa: BLE001 - listing skips corrupt files
                continue
        return out

    def iter_payloads(self) -> Iterator[Path]:  # pragma: no cover - utility
        yield from sorted(self.root.glob("*.json"))


def build_meta(
    series: OhlcvSeries,
    *,
    provider_symbol: str,
    fetched_at: datetime | None = None,
    gaps: list[str] | None = None,
    notes: str = "",
) -> DatasetMeta:
    """Derive a provenance record from a fetched/validated series."""
    if not series.bars:
        raise ValueError("cannot build metadata for an empty series")
    return DatasetMeta(
        dataset_id=compute_dataset_id(
            series.source, series.asset_id, series.timeframe,
            series.bars[0].timestamp, series.bars[-1].timestamp,
        ),
        content_sha256=bars_content_hash(series.bars),
        asset_id=series.asset_id,
        timeframe=series.timeframe,
        source=series.source,
        provider_symbol=provider_symbol,
        first_bar_timestamp=series.bars[0].timestamp,
        last_bar_timestamp=series.bars[-1].timestamp,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        bar_count=len(series.bars),
        gaps=list(gaps or []),
        notes=notes,
    )


__all__ = [
    "DatasetMeta",
    "HistoricalDataStore",
    "JsonDataStore",
    "build_meta",
    "compute_dataset_id",
    "bars_content_hash",
]
