"""Pre-execution signal validation gate.

Runs *after* research and duplicate suppression, *before* the risk engine.
Every check is deterministic; the first failure wins and is reported with a
stable machine-readable ``reason_code``. Invalid signals are rejected and
logged — never silently executed, never repaired (ARCHITECTURE.md P3.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from tradingagents.assets.registry import UnknownAssetError, get_asset
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.paper.models import PaperSignalRecord
from tradingagents.research.schemas import SignalAction

FUTURE_TOLERANCE_SECONDS = 300  # tolerate modest clock skew


@dataclass(frozen=True)
class SignalValidation:
    """Outcome of the validation gate."""

    ok: bool
    reason_code: str = ""
    detail: str = ""


def _fail(reason_code: str, detail: str) -> SignalValidation:
    return SignalValidation(ok=False, reason_code=reason_code, detail=detail)


def validate_signal_record(
    record: PaperSignalRecord,
    *,
    now: datetime,
    stale_overrides_hours: Mapping[str, float] | None = None,
    signal_exists: bool = False,
) -> SignalValidation:
    """Ordered deterministic checks over one generated signal record."""
    if now.tzinfo is None or record.generated_at.tzinfo is None:
        return _fail("invalid_timestamp", "timestamps must be timezone-aware")

    # 1. supported asset
    try:
        get_asset(record.asset_id)
    except (UnknownAssetError, KeyError):
        return _fail("unsupported_asset", f"asset {record.asset_id!r} not in registry")

    # 2. supported timeframe
    try:
        timeframe = Timeframe(record.timeframe.lower())
    except ValueError:
        return _fail("unsupported_timeframe", f"timeframe {record.timeframe!r} unknown")

    # 3. timestamp sanity (no future signals beyond skew tolerance)
    tolerance = timedelta(seconds=FUTURE_TOLERANCE_SECONDS)
    if record.generated_at > now + tolerance:
        return _fail("future_timestamp", "signal claims to be from the future")
    if record.decision_bar_close > record.generated_at + tolerance:
        return _fail("future_market_data", "decision bar closes after generation time")

    # 4. freshness of the underlying market data
    if record.market_data_timestamp is None:
        return _fail("stale_data", "market data timestamp missing")
    effective_close = record.market_data_timestamp + timedelta(minutes=timeframe.minutes)
    age_hours = (record.generated_at - effective_close).total_seconds() / 3600
    limit_hours = (stale_overrides_hours or {}).get(
        timeframe.value, timeframe.staleness_hours()
    )
    if age_hours < -tolerance.total_seconds() / 3600:
        return _fail("future_market_data", "market data newer than generation time")
    if age_hours > limit_hours:
        return _fail(
            "stale_data",
            f"market data {age_hours:.1f}h old exceeds {limit_hours:.1f}h limit "
            f"for {timeframe.value}",
        )

    # 5. price sanity
    for label, price in (
        ("entry_reference", record.entry_reference),
        ("stop_loss_reference", record.stop_loss_reference),
        ("take_profit_reference", record.take_profit_reference),
    ):
        if price is not None and price <= 0:
            return _fail("invalid_price", f"{label} must be positive, got {price}")

    # 6. SL/TP logical validity relative to action side
    if record.action in (SignalAction.BUY, SignalAction.SELL):
        entry = record.entry_reference
        stop = record.stop_loss_reference
        target = record.take_profit_reference
        if entry is None:
            return _fail("invalid_levels", "directional signal without entry reference")
        if stop is None:
            return _fail("missing_stop_level", "directional signal without stop loss")
        if record.action is SignalAction.BUY:
            if stop >= entry:
                return _fail("invalid_levels", f"BUY stop {stop} must be below entry {entry}")
            if target is not None and target <= entry:
                return _fail(
                    "invalid_levels", f"BUY target {target} must be above entry {entry}"
                )
        else:
            if stop <= entry:
                return _fail("invalid_levels", f"SELL stop {stop} must be above entry {entry}")
            if target is not None and target >= entry:
                return _fail(
                    "invalid_levels", f"SELL target {target} must be below entry {entry}"
                )

    # 7. confidence bounds (schema enforces too; belt-and-braces)
    if not 0.0 <= record.confidence <= 1.0:
        return _fail("invalid_confidence", "confidence outside [0, 1]")

    # 8. duplicate protection
    if signal_exists:
        return _fail("duplicate_signal", f"signal {record.signal_id} already processed")

    return SignalValidation(ok=True)


__all__ = ["SignalValidation", "validate_signal_record"]
