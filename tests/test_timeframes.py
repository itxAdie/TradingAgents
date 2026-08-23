"""Unit tests: timeframe representation."""

from __future__ import annotations

import pytest

from tradingagents.marketdata.timeframes import Timeframe


@pytest.mark.unit
class TestTimeframeEnum:
    def test_members_and_values(self):
        assert [tf.value for tf in Timeframe] == ["15m", "1h", "4h", "1d"]

    def test_minutes_monotonic(self):
        minutes = [tf.minutes for tf in Timeframe]
        assert minutes == sorted(minutes)

    def test_yfinance_intervals(self):
        assert Timeframe.M15.yfinance_interval == "15m"
        assert Timeframe.H1.yfinance_interval == "60m"
        assert Timeframe.D1.yfinance_interval == "1d"
        # Yahoo has no 4h interval; H4 must be resampled from hourly.
        assert Timeframe.H4.needs_resampling
        assert not Timeframe.D1.needs_resampling

    def test_parse_from_string(self):
        assert Timeframe("1h") is Timeframe.H1
        with pytest.raises(ValueError):
            Timeframe("2h")  # unsupported timeframe must be rejected loudly

    def test_history_caps_sane(self):
        assert Timeframe.M15.max_history_days() < Timeframe.H1.max_history_days()
        assert all(tf.max_history_days() > 0 for tf in Timeframe)
