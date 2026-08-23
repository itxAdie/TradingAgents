"""Unit tests: structured research logging (incl. secret hygiene)."""

from __future__ import annotations

import json
import logging

import pytest

from tradingagents.research.logging import log_event


@pytest.fixture(autouse=True)
def _capture_info(caplog):
    """Raise the research logger to INFO for every test here."""
    caplog.set_level(logging.INFO, logger="tradingagents.research")
    return caplog


def _events(caplog):
    out = []
    for r in caplog.records:
        try:
            out.append(json.loads(r.getMessage()))
        except json.JSONDecodeError:
            continue
    return out


@pytest.mark.unit
class TestLogEvent:
    def test_event_payload_shape(self, caplog):
        log_event("research_started", asset_id="XAUUSD", timeframe="1h")
        events = _events(caplog)
        assert events and events[0]["event"] == "research_started"
        assert events[0]["asset_id"] == "XAUUSD"
        assert "ts" in events[0]

    def test_datetimes_isoformatted(self, caplog):
        from datetime import datetime, timezone

        log_event("data_fetched", latest_bar=datetime(2026, 8, 23, tzinfo=timezone.utc))
        assert _events(caplog)[0]["latest_bar"].startswith("2026-08-23T00:00:00")

    def test_secrets_never_logged(self, caplog):
        log_event(
            "agent_started",
            api_key="sk-super-secret",
            API_KEY="also-secret",
            token="t",
            agent="news_analyst",
        )
        events = _events(caplog)
        assert len(events) == 1
        raw = json.dumps(events[0])
        assert "secret" not in raw
        assert events[0]["agent"] == "news_analyst"

    def test_unserializable_values_stringified(self, caplog):
        class Weird:
            def __repr__(self):
                return "<weird>"

        log_event("x", obj=Weird())
        assert _events(caplog)[0]["obj"] == "<weird>"
