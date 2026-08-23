"""Centralized timeframe representation.

Timeframes are a domain concept — never hardcoded inside agents. The enum
members are the only supported intervals in Phase 1; adding more later is an
enum row plus (if needed) a resample rule, not a call-site change.
"""

from __future__ import annotations

from enum import Enum


class Timeframe(str, Enum):
    """Supported research timeframes.

    Values are the canonical serialization ("15m", "1h", "4h", "1d").
    """

    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def minutes(self) -> int:
        """Bar length in minutes."""
        return {
            Timeframe.M15: 15,
            Timeframe.H1: 60,
            Timeframe.H4: 240,
            Timeframe.D1: 1440,
        }[self]

    @property
    def yfinance_interval(self) -> str:
        """Interval string accepted by ``yf.Ticker.history(interval=...)``.

        Yahoo has no native ``4h`` interval, so H4 is built by resampling
        H1 bars (see the Yahoo provider).
        """
        return {
            Timeframe.M15: "15m",
            Timeframe.H1: "60m",
            Timeframe.H4: "60m",  # fetch hourly; resample to 4h upstream
            Timeframe.D1: "1d",
        }[self]

    @property
    def needs_resampling(self) -> bool:
        """True when the provider must aggregate a smaller interval."""
        return self is Timeframe.H4

    def max_history_days(self) -> int:
        """Conservative lookback cap imposed by Yahoo intraday retention.

        15m data is retained ~60 days and hourly ~730 days on Yahoo; D1 is
        effectively unlimited but we keep the same interface for callers.
        """
        return {
            Timeframe.M15: 55,
            Timeframe.H1: 720,
            Timeframe.H4: 720,
            Timeframe.D1: 3650,
        }[self]

    def staleness_hours(self) -> float:
        """How old the newest bar may be (vs now) before data counts as stale.

        Generous multiples of the bar length so weekends/holidays on
        exchange-traded assets do not false-positive; used by the research
        engine to refuse signals from unknown-age data.
        """
        return {
            Timeframe.M15: 6 * 24,
            Timeframe.H1: 7 * 24,
            Timeframe.H4: 8 * 24,
            Timeframe.D1: 10 * 24,
        }[self]
