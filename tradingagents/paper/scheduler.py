"""Pure schedule math — no threads, no framework.

The scheduler decides *when a slot is due* from the last processed bar's
effective close (persisted per schedule key), not from wall-clock alone: if
no new bar has closed, there is nothing meaningful to analyse and the cycle
is skipped. This is what prevents burning AI calls between meaningful
market-data updates (ARCHITECTURE.md P3.1). The long-running loop in the CLI
is a thin wrapper around these pure functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tradingagents.paper.config import PaperTradingConfig, ScheduleEntry


@dataclass(frozen=True)
class ScheduleKey:
    """Identity of one asset/timeframe slot."""

    asset_id: str
    timeframe: str

    def value(self) -> str:
        return f"{self.asset_id}:{self.timeframe}"


def next_run_after(
    *, last_processed_bar_close: datetime, tf_minutes: int, offset_minutes: int
) -> datetime:
    """Advisory earliest run time after a processed bar."""
    return last_processed_bar_close + timedelta(minutes=tf_minutes + offset_minutes)


def is_due(
    *,
    last_processed_bar_close: datetime | None,
    tf_minutes: int,
    offset_minutes: int,
    now: datetime,
) -> bool:
    """A slot is due when never processed or past its advisory time.

    The engine independently verifies that a *new closed bar* exists before
    researching; this check only avoids pointless provider calls.
    """
    if last_processed_bar_close is None:
        return True
    return now >= next_run_after(
        last_processed_bar_close=last_processed_bar_close,
        tf_minutes=tf_minutes,
        offset_minutes=offset_minutes,
    )


def select_due_entries(
    config: PaperTradingConfig, now: datetime
) -> list[tuple[ScheduleEntry, str]]:
    """Enabled slots in deterministic order.

    Advisory due-ness (``is_due`` against persisted bookkeeping) and actual
    data novelty (new closed bar) are applied by the engine per slot.
    """
    from tradingagents.marketdata.timeframes import Timeframe

    return [
        (entry, ScheduleKey(entry.asset_id, Timeframe(entry.timeframe).value).value())
        for entry in config.schedules
        if entry.enabled
    ]


__all__ = ["ScheduleKey", "is_due", "next_run_after", "select_due_entries"]
