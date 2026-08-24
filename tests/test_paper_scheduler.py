"""Schedule math (paper/scheduler.py) — pure functions, no threads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.paper_helpers import default_config
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.paper.scheduler import (
    ScheduleKey,
    is_due,
    next_run_after,
    select_due_entries,
)

NOW = datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)


class TestNextRun:
    def test_hourly_offset_one_minute(self) -> None:
        nxt = next_run_after(
            last_processed_bar_close=NOW,
            tf_minutes=60,
            offset_minutes=1,
        )
        assert nxt == NOW + timedelta(minutes=61)

    def test_is_due_never_processed(self) -> None:
        assert is_due(last_processed_bar_close=None, tf_minutes=60, offset_minutes=1, now=NOW)

    def test_is_due_respects_offset(self) -> None:
        kwargs = {"tf_minutes": 60, "offset_minutes": 1}
        assert not is_due(last_processed_bar_close=NOW, now=NOW + timedelta(minutes=30), **kwargs)
        assert is_due(last_processed_bar_close=NOW, now=NOW + timedelta(minutes=61), **kwargs)


class TestSelectDueEntries:
    def test_enabled_only_deterministic_order(self) -> None:
        cfg = default_config(
            schedules=[
                _entry("BTCUSD", "15m", enabled=False),
                _entry("XAUUSD", "1h"),
                _entry("XAUUSD", "4h"),
            ]
        )
        due = select_due_entries(cfg, NOW)
        keys = [key for _, key in due]
        assert keys == ["XAUUSD:1h", "XAUUSD:4h"]

    def test_key_format(self) -> None:
        assert ScheduleKey("XAUUSD", Timeframe.H1.value).value() == "XAUUSD:1h"


def _entry(asset: str, tf: str, *, enabled: bool = True):
    from tradingagents.paper.config import ScheduleEntry

    return ScheduleEntry(asset_id=asset, timeframe=tf, enabled=enabled)
