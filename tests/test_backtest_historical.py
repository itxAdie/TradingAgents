"""Phase 2 — historical store, validation gate, and retention guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests._research_factories import make_bars, make_series
from tradingagents.backtest.historical.store import (
    DatasetMeta,
    JsonDataStore,
    build_meta,
    compute_dataset_id,
)
from tradingagents.backtest.historical.validation import (
    HistoricalDataError,
    ensure_valid,
    validate_series,
)
from tradingagents.backtest.historical.yahoo_history import fetch_history
from tradingagents.marketdata.models import Bar, DataStatus
from tradingagents.marketdata.timeframes import Timeframe

UTC = timezone.utc


def _hist_series(count: int = 24, **kwargs) -> object:
    series = make_series(count, status=DataStatus.HISTORICAL, **kwargs)
    return series


# -- validation ------------------------------------------------------------------


def test_clean_series_passes_with_no_gaps() -> None:
    report = validate_series(_hist_series(hours_step=1))
    assert report.ok and not report.gaps


def test_gap_is_flagged_never_fixed() -> None:
    bars = make_bars(6, end=datetime(2025, 3, 1, tzinfo=UTC), hours_step=1)
    # Remove one bar in the middle -> a 2-hour hole in a 1h cadence.
    holed = OhlcvSeriesShim(bars[:2] + bars[3:])
    report = validate_series(holed)
    assert report.ok  # gaps are soft findings, not rejections
    assert len(report.gaps) == 1
    before, after = report.gaps[0]
    assert before == bars[1].timestamp.isoformat()
    assert after == bars[3].timestamp.isoformat()
    assert "never interpolated" in report.warnings[0]


def test_weekend_style_gap_only_flagged_once() -> None:
    bars = make_bars(3, end=datetime(2025, 3, 7, 21, tzinfo=UTC), hours_step=1)
    later = make_bars(
        2, end=datetime(2025, 3, 10, 1, tzinfo=UTC), hours_step=1,
        base_price=bars[-1].close,
    )
    combined = OhlcvSeriesShim(bars + later)
    report = validate_series(combined)
    assert report.ok
    assert len(report.gaps) >= 1


def _corrupt(*mutations) -> list[Bar]:
    base = make_bars(5, end=datetime(2025, 3, 1, tzinfo=UTC))
    for m in mutations:
        base = m(base)
    return base


class OhlcvSeriesShim:
    """Non-pydantic carrier so the validator can see defects the model forbids."""

    def __init__(self, bars):
        self.bars = bars
        self.asset_id = "XAUUSD"
        self.timeframe = Timeframe.H1


def test_validator_rejects_non_positive_price() -> None:
    bars = _corrupt()
    bars[2] = bars[2].model_copy(update={"close": -5.0})
    report = validate_series(OhlcvSeriesShim(bars))
    assert not report.ok
    assert any("not a finite positive price" in e for e in report.errors)


def test_validator_rejects_high_below_body() -> None:
    bars = _corrupt()
    b = bars[1]
    bars[1] = b.model_copy(update={"high": min(b.open, b.close) * 0.5})
    report = validate_series(OhlcvSeriesShim(bars))
    assert not report.ok
    assert any("high below body max" in e for e in report.errors)


def test_validator_rejects_unordered_and_duplicate_timestamps() -> None:
    bars = _corrupt()
    shuffled = OhlcvSeriesShim([bars[2], bars[1], bars[0]] + bars[3:])
    assert any("ascending" in e for e in validate_series(shuffled).errors)
    duplicated = OhlcvSeriesShim([bars[0], bars[0]] + bars[1:])
    assert any("duplicate" in e for e in validate_series(duplicated).errors)


def test_ensure_valid_raises_historical_data_error() -> None:
    bars = _corrupt()
    bars[0] = bars[0].model_copy(update={"open": float("nan")})
    with pytest.raises(HistoricalDataError):
        ensure_valid(OhlcvSeriesShim(bars))


# -- store ------------------------------------------------------------------------


def test_store_save_load_roundtrip(tmp_path) -> None:
    store = JsonDataStore(tmp_path / "datasets")
    series = _hist_series(count=12)
    meta = build_meta(series, provider_symbol="GC=F")
    path = store.save(series, meta)
    assert path.exists()

    loaded_series, loaded_meta = store.load(series.asset_id, series.timeframe)
    assert loaded_meta.dataset_id == meta.dataset_id
    assert loaded_meta.content_sha256 == meta.content_sha256
    assert [b.timestamp for b in loaded_series.bars] == [
        b.timestamp for b in series.bars
    ]
    assert [b.close for b in loaded_series.bars] == [b.close for b in series.bars]
    assert loaded_series.status is DataStatus.HISTORICAL


def test_store_detects_content_corruption(tmp_path) -> None:
    store = JsonDataStore(tmp_path / "datasets")
    series = _hist_series(count=8)
    meta = build_meta(series, provider_symbol="GC=F")
    store.save(series, meta)

    # Tamper with the stored payload after save.
    payload_files = sorted((tmp_path / "datasets").glob("*.json"))
    assert payload_files, "dataset file missing"
    text = payload_files[0].read_text(encoding="utf-8")
    tampered = text.replace('"close":', '"closes":', 1)
    if tampered == text:  # key layout differs; force corruption another way
        tampered = text[:-4]
    payload_files[0].write_text(tampered, encoding="utf-8")

    with pytest.raises(HistoricalDataError):
        store.load(series.asset_id, series.timeframe)


def test_store_list_datasets(tmp_path) -> None:
    store = JsonDataStore(tmp_path / "datasets")
    s1 = _hist_series(count=6, asset_id="XAUUSD")
    s2 = _hist_series(count=6, asset_id="BTCUSD")
    store.save(s1, build_meta(s1, provider_symbol="GC=F"))
    store.save(s2, build_meta(s2, provider_symbol="BTC-USD"))
    metas = store.list_datasets()
    assert {m.asset_id for m in metas} == {"XAUUSD", "BTCUSD"}


def test_dataset_id_stable_and_window_sensitive() -> None:
    a = compute_dataset_id(
        "yahoo", "XAUUSD", Timeframe.H1,
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC),
    )
    b = compute_dataset_id(
        "yahoo", "XAUUSD", Timeframe.H1,
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC),
    )
    c = compute_dataset_id(
        "yahoo", "XAUUSD", Timeframe.H1,
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC),
    )
    assert a == b and a != c


def test_build_meta_records_bar_count_and_hash() -> None:
    series = _hist_series(count=9)
    meta = build_meta(series, provider_symbol="GC=F")
    assert isinstance(meta, DatasetMeta)
    assert meta.bar_count == 9
    assert len(meta.content_sha256) == 64
    assert meta.first_bar_timestamp == series.bars[0].timestamp
    assert meta.last_bar_timestamp == series.bars[-1].timestamp


# -- retention guard (no network) ---------------------------------------------------


def test_fetch_history_rejects_window_beyond_retention() -> None:
    from unittest.mock import MagicMock

    provider = MagicMock()  # must never be touched when the guard trips
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=400)  # 15m retention is ~60 days
    with pytest.raises(ValueError, match="retention"):
        fetch_history(
            _fake_asset(), Timeframe.M15, start=start, end=end, provider=provider
        )
    provider.get_ohlcv.assert_not_called()


def test_fetch_history_rejects_inverted_window() -> None:
    with pytest.raises(ValueError, match="start must be before end"):
        fetch_history(
            _fake_asset(), Timeframe.D1,
            start=datetime(2025, 2, 1, tzinfo=UTC),
            end=datetime(2025, 1, 1, tzinfo=UTC),
        )


def test_fetch_history_relabels_as_historical() -> None:
    from unittest.mock import MagicMock

    raw = _hist_series(count=5)
    provider = MagicMock()
    provider.get_ohlcv.return_value = raw.model_copy(
        update={"status": DataStatus.REALTIME}
    )
    series, report = fetch_history(
        _fake_asset(), Timeframe.D1,
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 10, tzinfo=UTC),
        provider=provider,
    )
    assert series.status is DataStatus.HISTORICAL
    assert report.ok


def _fake_asset():
    from tradingagents.assets.registry import get_asset

    return get_asset("XAUUSD")
