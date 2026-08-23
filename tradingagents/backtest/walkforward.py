"""Walk-forward window generation and aggregation.

Splits a bar timeline into consecutive, non-overlapping frames:

    [ train | validation | test ]
            [ train | validation | test ]
                    ...advancing by ``step_bars``...

Rules (ARCHITECTURE.md P2.6 / spec §5):
- windows are index ranges over ONE dataset; each frame's runner receives
  only its own slice, so future test data is structurally invisible;
- ``min_bars`` guards against degenerate windows (skipped, reported);
- per-window results stay independent; aggregation happens only after all
  windows complete.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WalkForwardConfig(BaseModel):
    train_bars: int = Field(default=300, ge=10)
    validation_bars: int = Field(default=100, ge=0)
    test_bars: int = Field(default=100, ge=1)
    step_bars: int = Field(default=100, ge=1)
    min_bars: int = Field(default=50, ge=1)

    def frame_size(self) -> int:
        return self.train_bars + self.validation_bars + self.test_bars


class WindowFrame(BaseModel):
    """One walk-forward period as bar-index ranges (half-open [start, end))."""

    window_id: int
    train_start: int
    train_end: int  # exclusive; == validation_start
    validation_end: int  # exclusive; == test_start
    test_end: int  # exclusive


def generate_windows(n_bars: int, cfg: WalkForwardConfig) -> list[WindowFrame]:
    """All complete frames fitting in ``n_bars`` bars."""
    size = cfg.frame_size()
    if n_bars < size:
        return []
    frames: list[WindowFrame] = []
    start = 0
    wid = 1
    while start + size <= n_bars:
        frame = WindowFrame(
            window_id=wid,
            train_start=start,
            train_end=start + cfg.train_bars,
            validation_end=start + cfg.train_bars + cfg.validation_bars,
            test_end=start + size,
        )
        # min_bars applies to each phase that will actually be used.
        if (
            cfg.train_bars >= cfg.min_bars
            and cfg.test_bars >= min(cfg.min_bars, cfg.test_bars)
        ):
            frames.append(frame)
        start += cfg.step_bars
        wid += 1
    return frames


class WindowResult(BaseModel):
    """Independent per-window outcome (filled by the runner)."""

    window_id: int
    strategy_id: str
    train_period: tuple[str, str]
    validation_period: tuple[str, str] | None = None
    test_period: tuple[str, str]
    trades: int
    total_return_pct: float | None = None
    max_drawdown_pct: float | None = None
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    skipped_reason: str | None = None


class WalkForwardAggregate(BaseModel):
    """Cross-window summary computed only after every window finished."""

    strategy_id: str
    n_windows: int = 0
    profitable_windows: int = 0
    losing_windows: int = 0
    pct_profitable: float | None = None
    average_window_return_pct: float | None = None
    median_window_return_pct: float | None = None
    best_window_return_pct: float | None = None
    worst_window_return_pct: float | None = None
    aggregate_return_pct: float | None = None
    aggregate_max_drawdown_pct: float | None = None
    consistency_note: str = ""
    overfitting_diagnostics: dict[str, str] = Field(default_factory=dict)


def aggregate_window_results(
    results: list[WindowResult],
) -> WalkForwardAggregate:
    """Deterministic cross-window statistics (+ overfitting evidence only)."""
    agg = WalkForwardAggregate(
        strategy_id=results[0].strategy_id if results else "unknown"
    )
    usable = [
        r for r in results
        if r.skipped_reason is None and r.total_return_pct is not None
    ]
    agg.n_windows = len(results)
    if not usable:
        agg.consistency_note = "no completed windows to aggregate"
        return agg
    rets = sorted(r.total_return_pct for r in usable)
    mid = len(rets) // 2
    median = (
        rets[mid] if len(rets) % 2 else (rets[mid - 1] + rets[mid]) / 2
    )
    agg.profitable_windows = sum(1 for r in rets if r > 0)
    agg.losing_windows = sum(1 for r in rets if r <= 0)
    agg.pct_profitable = agg.profitable_windows / len(usable) * 100
    agg.average_window_return_pct = sum(rets) / len(rets)
    agg.median_window_return_pct = median
    agg.best_window_return_pct = rets[-1]
    agg.worst_window_return_pct = rets[0]
    dd_max = [r.max_drawdown_pct for r in usable if r.max_drawdown_pct is not None]
    agg.aggregate_max_drawdown_pct = max(dd_max) if dd_max else None

    diagnostics: dict[str, str] = {}
    if len(usable) < 3:
        diagnostics["window_count"] = (
            f"only {len(usable)} completed window(s); stability claims unsupported"
        )
    std_like = (rets[-1] - rets[0]) / abs(agg.average_window_return_pct or 1.0)
    if std_like > 4:
        diagnostics["dispersion"] = (
            "best/worst spread exceeds 4x the mean magnitude; performance unstable"
        )
    low_trades = [r for r in usable if r.trades < 5]
    if low_trades:
        diagnostics["trade_count"] = (
            f"{len(low_trades)} window(s) with <5 trades; their metrics are noisy"
        )
    agg.overfitting_diagnostics = diagnostics
    return agg


__all__ = [
    "WalkForwardAggregate",
    "WalkForwardConfig",
    "WindowFrame",
    "WindowResult",
    "aggregate_window_results",
    "generate_windows",
]
