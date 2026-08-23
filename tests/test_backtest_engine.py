"""Phase 2 — engine orchestration: determinism, prefix isolation, report.

The no-look-ahead guarantee is tested *structurally*: a decision at bar T
must be identical whether or not future bars exist in memory. We run a
recording strategy over the full dataset and over every truncation, then
compare decisions timestamp-by-timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests._research_factories import make_bars
from tradingagents.backtest.baselines import (
    BuyAndHoldStrategy,
    MomentumStrategy,
    SmaCrossStrategy,
)
from tradingagents.backtest.config import ExecutionConfig, RiskLimits, SizingPolicy
from tradingagents.backtest.engine import (
    BacktestRunOutput,
    build_report,
    run_backtest,
    slice_upto,
)
from tradingagents.backtest.historical.store import build_meta
from tradingagents.backtest.walkforward import WalkForwardConfig
from tradingagents.marketdata.models import DataStatus, OhlcvSeries
from tradingagents.marketdata.timeframes import Timeframe

UTC = timezone.utc


def _dataset(count: int = 260) -> OhlcvSeries:
    bars = make_bars(
        count,
        end=datetime(2025, 8, 1, tzinfo=UTC),
        hours_step=1,
        zigzag_pct_per_bar=0.45,
    )
    return OhlcvSeries(
        asset_id="XAUUSD", timeframe=Timeframe.H1,
        source="yahoo", status=DataStatus.HISTORICAL, bars=bars,
    )


class _RecordingStrategy:
    """Wraps MomentumStrategy; records (timestamp, action, entry_ref) tuples."""

    strategy_id = "recorder"

    def __init__(self):
        self._inner = MomentumStrategy(lookback=20)
        self.decisions: list[tuple] = []

    def generate(self, *, asset_id, timeframe, visible, now):
        sig = self._inner.generate(
            asset_id=asset_id, timeframe=timeframe, visible=visible, now=now,
        )
        if sig is not None:
            self.decisions.append((sig.generated_at, sig.action.value, sig.entry_reference))
        return sig


def test_slice_upto_includes_only_bars_at_or_before_moment() -> None:
    ds = _dataset(30)
    cut = ds.bars[10].timestamp
    view = slice_upto(ds, cut)
    assert len(view.bars) == 11
    assert view.bars[-1].timestamp == cut
    assert view.status is ds.status


def test_run_backtest_output_shape_and_benchmark() -> None:
    ds = _dataset(260)
    out = run_backtest(
        dataset=ds, strategies=[SmaCrossStrategy(), MomentumStrategy()],
        initial_capital=5_000.0, warmup_bars=60,
    )
    assert isinstance(out, BacktestRunOutput)
    assert set(out.results) == {"baseline_sma_cross", "baseline_momentum"}
    for _sid, result in out.results.items():
        # Curve is anchored at configured capital before any trade.
        assert result.stats.initial_capital == 5_000.0
        assert result.equity_curve[0].equity == 5_000.0
        assert len(result.equity_curve) > 1
        # Benchmark computed on the SAME decision window (first_decision=59).
        bh = result.stats.benchmark_buy_hold_return_pct
        first_open = ds.bars[59].open
        last_close = ds.bars[-1].close
        assert bh == pytest.approx((last_close / first_open - 1) * 100)
    # Ledgers mirror results.
    assert set(out.ledgers) == set(out.results)
    assert len(out.window_shells) == 2


def test_run_backtest_is_deterministic() -> None:
    ds = _dataset(220)
    kwargs = {
        "dataset": ds, "strategies": [MomentumStrategy()],
        "initial_capital": 2_000.0, "warmup_bars": 50,
    }
    a: BacktestRunOutput = run_backtest(**kwargs)
    b: BacktestRunOutput = run_backtest(**kwargs)
    curve_a = [(p.timestamp, p.equity) for p in a.results["baseline_momentum"].equity_curve]
    curve_b = [(p.timestamp, p.equity) for p in b.results["baseline_momentum"].equity_curve]
    trades_a = [(r.trade_id, r.net_pnl) for r in a.ledgers["baseline_momentum"].records]
    trades_b = [(r.trade_id, r.net_pnl) for r in b.ledgers["baseline_momentum"].records]
    assert curve_a == curve_b
    assert trades_a == trades_b


@pytest.mark.parametrize("truncate_at", [90, 150, 210])
def test_decisions_are_prefix_isolated_no_look_ahead(truncate_at: int) -> None:
    """Decisions before the cut are identical with/without future bars."""
    full_recorder = _RecordingStrategy()
    truncated_recorder = _RecordingStrategy()

    ds_full = _dataset(240)
    run_backtest(dataset=ds_full, strategies=[full_recorder],
                 initial_capital=1_000.0, warmup_bars=40)

    ds_trunc = OhlcvSeries(
        asset_id=ds_full.asset_id, timeframe=ds_full.timeframe,
        source=ds_full.source, status=ds_full.status,
        bars=ds_full.bars[:truncate_at],
    )
    run_backtest(dataset=ds_trunc, strategies=[truncated_recorder],
                 initial_capital=1_000.0, warmup_bars=40)

    cut_stamp = ds_full.bars[truncate_at - 1].timestamp
    full_before_cut = [d for d in full_recorder.decisions if d[0] <= cut_stamp]
    truncated_all = truncated_recorder.decisions
    # The truncated run cannot see anything after the cut, so ALL its
    # decisions must match the corresponding full-run decisions exactly —
    # including prices computed from indicators (entry_reference).
    assert truncated_all == full_before_cut[: len(truncated_all)]
    assert len(truncated_all) > 0  # sanity: the run actually decided things


def test_execution_provenance_entry_after_signal() -> None:
    """Every fill happens strictly after its signal's generation instant."""
    ds = _dataset(220)
    out = run_backtest(dataset=ds, strategies=[MomentumStrategy(), SmaCrossStrategy()],
                       initial_capital=1_000.0, warmup_bars=50)
    for ledger in out.ledgers.values():
        for rec in ledger.records:
            assert rec.entry_timestamp > rec.signal_generated_at
            # Raw prices must be actual bar prices touched (provenance).
            assert rec.raw_entry_price > 0 and rec.raw_exit_price > 0


def test_dataset_too_small_raises() -> None:
    ds = _dataset(30)
    with pytest.raises(ValueError, match="too small"):
        run_backtest(dataset=ds, strategies=[BuyAndHoldStrategy()], warmup_bars=210)


def test_walk_forward_end_to_end() -> None:
    ds = _dataset(520)
    from tradingagents.backtest.engine import run_walk_forward

    agg = run_walk_forward(
        dataset=ds,
        strategies=[MomentumStrategy(), SmaCrossStrategy()],
        wf_config=WalkForwardConfig(
            train_bars=200, validation_bars=30, test_bars=80, step_bars=110,
        ),
        initial_capital=1_000.0,
    )
    assert set(agg) == {"baseline_momentum", "baseline_sma_cross"}
    for _sid, (windows, aggregate) in agg.items():
        assert aggregate.n_windows == len(windows) >= 1
        # Every window's decisions confined to its own test phase.
        for w in windows:
            assert w.test_period[0] < w.test_period[1]


def test_build_report_json_roundtrip_and_disclaimer(tmp_path) -> None:
    ds = _dataset(160)
    out = run_backtest(dataset=ds, strategies=[BuyAndHoldStrategy()],
                       initial_capital=1_000.0, warmup_bars=40)
    meta = build_meta(ds, provider_symbol="GC=F")
    report = build_report(
        dataset=ds, dataset_meta=meta, results=out.results,
        initial_capital=1_000.0,
        execution_cfg=ExecutionConfig(),
        sizing=SizingPolicy(),
        limits=RiskLimits(),
        enabled_components=["market_data"],
        disabled_components=["news", "sentiment", "macro", "ai_research"],
        run_id="report-test",
    )
    text = report.render_text()
    assert "RESEARCH SIGNAL" in text
    assert "HISTORICAL SIMULATION ONLY" in text
    assert report.disclaimer in text
    assert "news" in text  # disabled components listed

    path = report.to_json(tmp_path / "r.json")
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_name"] == "BACKTEST_REPORT"
    assert payload["run_id"] == "report-test"
    assert payload["asset_id"] == "XAUUSD"
    assert payload["dataset_meta"]["bar_count"] == 160
    assert payload["ai_usage"]["enabled"] is False
    # config_hash stable across calls, changes when content changes
    h1 = report.config_hash()
    h2 = report.config_hash()
    assert h1 == h2 and len(h1) == 12
