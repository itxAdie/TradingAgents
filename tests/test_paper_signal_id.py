"""Content-based signal identity (paper/signal_id.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.paper_helpers import T0, make_bar
from tradingagents.paper.signal_id import (
    SIGNAL_ID_LENGTH,
    compute_config_hash,
    compute_signal_id,
    utc_iso,
    visible_bars_digest,
)

DECISION = T0 + timedelta(hours=4)


def _sig_id(**overrides):
    kwargs = {
        "asset_id": "XAUUSD",
        "timeframe": "1h",
        "decision_bar_close": DECISION,
        "visible_bars_hash": "hash-a",
        "model_ids": ["m1", "m2"],
        "config_hash": "cfg",
    }
    kwargs.update(overrides)
    return compute_signal_id(**kwargs)


class TestSignalId:
    def test_deterministic_across_calls_and_ordering(self) -> None:
        assert _sig_id() == _sig_id()
        assert _sig_id(model_ids=["m2", "m1"]) == _sig_id()

    def test_new_decision_bar_changes_identity(self) -> None:
        assert _sig_id() != _sig_id(
            decision_bar_close=DECISION + timedelta(hours=1)
        )

    def test_any_component_change_changes_identity(self) -> None:
        base = _sig_id()
        assert base != _sig_id(asset_id="BTCUSD")
        assert base != _sig_id(timeframe="4h")
        assert base != _sig_id(visible_bars_hash="hash-b")
        assert base != _sig_id(model_ids=["m1"])
        assert base != _sig_id(config_hash="cfg2")

    def test_asset_case_and_tf_case_insensitive(self) -> None:
        assert _sig_id(asset_id="xauusd") == _sig_id(asset_id="XAUUSD")
        assert _sig_id(timeframe="1H") == _sig_id(timeframe="1h")

    def test_length(self) -> None:
        assert len(_sig_id()) == SIGNAL_ID_LENGTH


class TestUtcIso:
    def test_requires_tz_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            utc_iso(datetime(2026, 8, 24, 12, 0))

    def test_normalizes_to_utc(self) -> None:
        plus3 = timezone(timedelta(hours=3))
        assert utc_iso(datetime(2026, 8, 24, 15, 0, tzinfo=plus3)) == utc_iso(
            datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        )


class TestVisibleBarsDigest:
    def test_content_sensitive_not_order_dependent_input(self) -> None:
        bars_a = [
            make_bar(T0, op=1, hi=2, lo=0.5, cl=1.5),
            make_bar(T0 + timedelta(hours=1), op=1.5, hi=2.5, lo=1.0, cl=2.0),
        ]
        bars_b = [
            make_bar(T0, op=1, hi=2, lo=0.5, cl=1.5),
            make_bar(T0 + timedelta(hours=1), op=1.5, hi=2.5, lo=1.0, cl=2.01),
        ]
        assert visible_bars_digest(bars_a) == visible_bars_digest(list(bars_a))
        assert visible_bars_digest(bars_a) != visible_bars_digest(bars_b)

    def test_prefix_invariance(self) -> None:
        """Adding a newer bar changes the digest (window grew)."""
        bars = [
            make_bar(T0 + timedelta(hours=i), op=10, hi=11, lo=9, cl=10.5)
            for i in range(3)
        ]
        d3 = visible_bars_digest(bars[:3])
        d4 = visible_bars_digest(
            bars[:3]
            + [make_bar(T0 + timedelta(hours=3), op=10.5, hi=12, lo=10, cl=11)]
        )
        assert d3 != d4


class TestConfigHash:
    def test_stable_and_order_insensitive(self) -> None:
        a = compute_config_hash({"quick_think_llm": "gpt", "deep_think_llm": "o3"})
        b = compute_config_hash({"deep_think_llm": "o3", "quick_think_llm": "gpt"})
        assert a == b
        assert len(a) == 12

    def test_value_change_detected(self) -> None:
        assert compute_config_hash({"k": "v"}) != compute_config_hash({"k": "w"})
