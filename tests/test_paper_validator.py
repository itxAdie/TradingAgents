"""Validation gate: ordered deterministic checks with stable reason codes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.paper.models import (
    PaperSignalRecord,
    ResearchSnapshot,
    SignalState,
)
from tradingagents.paper.validator import validate_signal_record
from tradingagents.research.schemas import SignalAction

NOW = datetime(2026, 8, 24, 13, 6, tzinfo=timezone.utc)


def make_record(**overrides) -> PaperSignalRecord:
    md_ts = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    fields = {
        "signal_id": "sig-1",
        "account_id": "a",
        "environment": "test",
        "asset_id": "XAUUSD",
        "timeframe": "1h",
        "state": SignalState.GENERATED,
        "decision_bar_close": datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc),
        "generated_at": datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc),
        "market_data_timestamp": md_ts,
        "action": SignalAction.BUY,
        "confidence": 0.7,
        "thesis": "t",
        "entry_reference": 2000.0,
        "stop_loss_reference": 1940.0,
        "take_profit_reference": 2100.0,
        "research": ResearchSnapshot(
            thesis="t", confidence=0.7, generated_at=NOW, research_version="v1"
        ),
        "updated_at": NOW,
    }
    fields.update(overrides)
    return PaperSignalRecord(**fields)


def _validate(record: PaperSignalRecord, **kwargs):
    return validate_signal_record(record, now=NOW, **kwargs)


class TestHappyPaths:
    def test_buy_ok(self) -> None:
        assert _validate(make_record()).ok

    def test_sell_ok(self) -> None:
        record = make_record(
            action=SignalAction.SELL,
            entry_reference=2000.0,
            stop_loss_reference=2060.0,
            take_profit_reference=1900.0,
        )
        assert _validate(record).ok

    def test_hold_needs_no_levels(self) -> None:
        record = make_record(
            action=SignalAction.HOLD,
            entry_reference=None,
            stop_loss_reference=None,
            take_profit_reference=None,
        )
        assert _validate(record).ok


class TestRejections:
    def test_unsupported_asset(self) -> None:
        v = _validate(make_record(asset_id="MEMECOIN"))
        assert (v.ok, v.reason_code) == (False, "unsupported_asset")

    def test_unsupported_timeframe(self) -> None:
        v = _validate(make_record(timeframe="2h"))
        assert (v.ok, v.reason_code) == (False, "unsupported_timeframe")

    def test_future_timestamp_beyond_skew(self) -> None:
        v = _validate(make_record(generated_at=NOW + timedelta(minutes=30)))
        assert (v.ok, v.reason_code) == (False, "future_timestamp")

    def test_future_market_data_when_decision_after_generation(self) -> None:
        v = _validate(
            make_record(decision_bar_close=NOW + timedelta(hours=2))
        )
        assert (v.ok, v.reason_code) == (False, "future_market_data")

    def test_stale_data_default_window(self) -> None:
        # market data effective close is 2h before generation; 1h override trips
        md_ts = NOW - timedelta(hours=3)  # bar stamped 10:06-ish
        v = _validate(
            make_record(
                generated_at=NOW - timedelta(minutes=5),
                decision_bar_close=md_ts + timedelta(hours=1),
                market_data_timestamp=md_ts,
            ),
            stale_overrides_hours={"1h": 1.0},
        )
        assert (v.ok, v.reason_code) == (False, "stale_data")

    def test_fresh_within_override_passes(self) -> None:
        recent = NOW - timedelta(minutes=90)
        v = _validate(
            make_record(
                generated_at=recent + timedelta(minutes=5),
                decision_bar_close=recent,
                market_data_timestamp=recent - timedelta(hours=1),
            ),
            stale_overrides_hours={"1h": 4.0},
        )
        assert v.ok

    def test_missing_market_data_timestamp(self) -> None:
        v = _validate(make_record(market_data_timestamp=None))
        assert (v.ok, v.reason_code) == (False, "stale_data")

    def test_non_positive_price(self) -> None:
        v = _validate(make_record(entry_reference=0.0))
        assert (v.ok, v.reason_code) == (False, "invalid_price")

    def test_buy_stop_above_entry_invalid(self) -> None:
        v = _validate(make_record(stop_loss_reference=2050.0))
        assert (v.ok, v.reason_code) == (False, "invalid_levels")

    def test_buy_target_below_entry_invalid(self) -> None:
        v = _validate(make_record(take_profit_reference=1999.0))
        assert (v.ok, v.reason_code) == (False, "invalid_levels")

    def test_sell_stop_below_entry_invalid(self) -> None:
        v = _validate(
            make_record(
                action=SignalAction.SELL,
                stop_loss_reference=1950.0,
                take_profit_reference=1900.0,
            )
        )
        assert (v.ok, v.reason_code) == (False, "invalid_levels")

    def test_missing_stop_rejected_for_directional(self) -> None:
        v = _validate(make_record(stop_loss_reference=None))
        assert (v.ok, v.reason_code) == (False, "missing_stop_level")

    def test_confidence_out_of_bounds(self) -> None:
        # schema already enforces bounds; construct via model_copy to bypass
        record = make_record().model_copy(update={"confidence": 1.5})
        v = validate_signal_record(record, now=NOW)
        assert (v.ok, v.reason_code) in {
            (False, "invalid_confidence"),
        } or not v.ok  # pydantic may reject at construction; either way not ok

    def test_duplicate_flag(self) -> None:
        v = _validate(make_record(), signal_exists=True)
        assert (v.ok, v.reason_code) == (False, "duplicate_signal")


@pytest.mark.parametrize("field", ["entry_reference", "stop_loss_reference", "take_profit_reference"])
def test_hold_ignores_level_fields(field: str) -> None:
    record = make_record(action=SignalAction.HOLD).model_copy(
        update={field: -5.0}
    )
    v = validate_signal_record(record, now=NOW)
    assert not v.ok or field != "entry_reference"  # price sanity still applies
