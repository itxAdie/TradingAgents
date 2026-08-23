"""Unit tests: research schemas and renderers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tradingagents.research.schemas import (
    DataSourceRef,
    ResearchSignal,
    RiskLevel,
    SignalAction,
    render_signal,
)


def _signal(**overrides) -> ResearchSignal:
    base = {
        "asset_id": "XAUUSD",
        "generated_at": datetime.now(timezone.utc),
        "timeframe": "1h",
        "action": SignalAction.BUY,
        "confidence": 0.7,
        "entry_reference": 2400.0,
        "stop_loss_reference": 2385.0,
        "take_profit_reference": 2430.0,
        "risk_level": RiskLevel.MEDIUM,
        "thesis": "Test thesis grounded in provided evidence.",
        "supporting_factors": ["trend=up"],
        "opposing_factors": ["news unavailable"],
        "invalidation_conditions": ["close below stop"],
        "data_sources": [
            DataSourceRef(name="yahoo", kind="market_data", status="delayed")
        ],
        "models_used": ["test-model"],
    }
    base.update(overrides)
    return ResearchSignal(**base)


@pytest.mark.unit
class TestResearchSignal:
    def test_disclaimer_is_immutable_default(self):
        s = _signal()
        assert s.disclaimer == "RESEARCH SIGNAL — NOT EXECUTED"
        assert s.signal_kind == "research"
        with pytest.raises(ValidationError):
            _signal(disclaimer="FREE MONEY")  # literal type rejects other values

    def test_actions_restricted(self):
        for action in ("BUY", "SELL", "HOLD"):
            assert _signal(action=action).action.value == action
        with pytest.raises(ValidationError):
            _signal(action="LAMBO")

    def test_confidence_bounds_and_rounding(self):
        assert _signal(confidence=0.123456).confidence == 0.1235
        with pytest.raises(ValidationError):
            _signal(confidence=1.5)
        with pytest.raises(ValidationError):
            _signal(confidence=-0.1)

    def test_levels_optional(self):
        s = _signal(entry_reference=None, stop_loss_reference=None, take_profit_reference=None)
        assert s.entry_reference is None


@pytest.mark.unit
class TestRender:
    def test_render_matches_brief_layout(self):
        text = render_signal(_signal())
        for heading in (
            "Asset:", "Time:", "Timeframe:", "Signal:", "Confidence:",
            "Entry Reference:", "Stop Reference:", "Target Reference:",
            "Risk:", "Invalidation:", "Data Sources:", "AI Models:",
            "RESEARCH SIGNAL — NOT EXECUTED",
        ):
            assert heading in text, f"missing {heading!r}"
        assert "BUY" in text
        assert "70%" in text  # confidence percentage rendering

    def test_render_handles_missing_levels(self):
        text = render_signal(_signal(
            action=SignalAction.HOLD,
            entry_reference=None, stop_loss_reference=None, take_profit_reference=None,
        ))
        assert "n/a" in text


@pytest.mark.unit
def test_data_source_ref_kinds_constrained():
    with pytest.raises(ValidationError):
        DataSourceRef(name="x", kind="broker_feed", status="ok")
