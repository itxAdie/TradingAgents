"""Deterministic historical-data quality gate (ARCHITECTURE.md P2.3).

Policy: **never silently fix questionable market data.**

- Hard defects (ordering violations, duplicates, non-UTC timestamps, invalid
  OHLC relationships, non-finite/non-positive prices) → the dataset is
  *rejected*: :class:`HistoricalDataError` lists every reason.
- Soft anomalies (missing candles / gaps vs. the timeframe's nominal
  interval) → *flagged* in ``ValidationReport.gaps`` and persisted in dataset
  metadata. Gaps are normal for real venues (weekends, holidays, halts);
  interpolation would fabricate prices, so they are surfaced instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from tradingagents.dataflows.errors import VendorError
from tradingagents.marketdata.models import OhlcvSeries


class HistoricalDataError(VendorError):
    """A historical dataset failed hard validation and was rejected."""


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    checked_bars: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    gaps: tuple[tuple[str, str], ...] = field(default=(), compare=False)

    def summary(self) -> str:
        status = "OK" if self.ok else "REJECTED"
        return (
            f"{status}: {self.checked_bars} bars, "
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), "
            f"{len(self.gaps)} gap(s)"
        )


# A spacing beyond this multiple of the nominal interval counts as a gap.
_GAP_MULTIPLIER = 1.5
_MAX_GAPS_REPORTED = 20


def validate_series(series: OhlcvSeries) -> ValidationReport:
    """Run every hard check; collect gaps as soft findings.

    Note: the ``OhlcvSeries`` model already enforces tz-aware timestamps,
    strict ordering, uniqueness, positive finite prices and uniform volume
    presence at construction — constructing a series with those defects is
    impossible. This validator therefore (a) re-states the invariants for
    datasets arriving through non-pydantic paths and (b) adds what the model
    cannot know: expected-cadence gap detection.
    """
    errors: list[str] = []
    warnings: list[str] = []
    gaps: list[tuple[str, str]] = []
    bars = series.bars
    nominal = series.timeframe.minutes * 60  # seconds

    for i, bar in enumerate(bars):
        for name in ("open", "high", "low", "close"):
            value = getattr(bar, name)
            if not math.isfinite(value) or value <= 0:
                errors.append(
                    f"bar {i} ({bar.timestamp.isoformat()}): {name}={value!r} "
                    "is not a finite positive price"
                )
        if bar.high < max(bar.open, bar.close) - 1e-9:
            errors.append(f"bar {i}: high below body max")
        if bar.low > min(bar.open, bar.close) + 1e-9:
            errors.append(f"bar {i}: low above body min")
        if bar.volume is not None and (
            not math.isfinite(bar.volume) or bar.volume < 0
        ):
            errors.append(f"bar {i}: volume={bar.volume!r} invalid")

    stamps = [b.timestamp for b in bars]
    if any(s.tzinfo is None for s in stamps):
        errors.append("naive (timezone-unaware) timestamps present")
    if any(later <= earlier for earlier, later in zip(stamps, stamps[1:], strict=False)):
        errors.append("timestamps are not strictly ascending")
    if len(set(stamps)) != len(stamps):
        errors.append("duplicate bar timestamps present")

    for earlier, later in zip(stamps, stamps[1:], strict=False):
        delta = (later - earlier).total_seconds()
        if delta > nominal * _GAP_MULTIPLIER:
            gaps.append((earlier.isoformat(), later.isoformat()))
    if gaps:
        shown = ", ".join(f"{a} -> {b}" for a, b in gaps[:_MAX_GAPS_REPORTED])
        more = f" (+{len(gaps) - _MAX_GAPS_REPORTED} more)" if len(gaps) > _MAX_GAPS_REPORTED else ""
        warnings.append(
            f"{len(gaps)} gap(s) vs nominal {series.timeframe.value} cadence; "
            f"gaps are flagged, never interpolated [{shown}{more}]"
        )

    return ValidationReport(
        ok=not errors,
        checked_bars=len(bars),
        errors=tuple(errors),
        warnings=tuple(warnings),
        gaps=tuple(gaps),
    )


def ensure_valid(series: OhlcvSeries) -> ValidationReport:
    """Validate or raise :class:`HistoricalDataError` with all reasons."""
    report = validate_series(series)
    if not report.ok:
        raise HistoricalDataError(
            f"historical dataset rejected for {series.asset_id} "
            f"@ {series.timeframe.value}: {report.summary()} :: "
            + "; ".join(report.errors[:10])
        )
    return report


__all__ = ["HistoricalDataError", "ValidationReport", "ensure_valid", "validate_series"]
