"""Backtesting orchestrator: one research engine, replayed over history.

The AI strategy path is *literally* the Phase 1 ``ResearchEngine`` running
against a :class:`~tradingagents.backtest.historical.replay_provider.ReplayMarketDataProvider`
with an injected :class:`~tradingagents.backtest.clock.SimulationClock`.
Baseline strategies consume the same point-in-time data slices and emit the
same :class:`~tradingagents.research.schemas.ResearchSignal` objects, so a
single executor evaluates every strategy identically.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradingagents.backtest.analytics import (
    EquityPoint,
    PerformanceStats,
    buy_and_hold_reference,
    compute_stats,
)
from tradingagents.backtest.clock import SimulationClock
from tradingagents.backtest.config import ExecutionConfig, RiskLimits, SizingPolicy
from tradingagents.backtest.execution import ExecutionSimulator
from tradingagents.backtest.historical.replay_provider import ReplayMarketDataProvider
from tradingagents.backtest.ledger import TradeLedger
from tradingagents.backtest.portfolio import Portfolio
from tradingagents.backtest.report import (
    AIUsage,
    BacktestReport,
    StrategyResult,
    git_commit,
)
from tradingagents.backtest.research_cache import PROMPT_VERSION, ResearchCache
from tradingagents.backtest.walkforward import (
    WalkForwardAggregate,
    WalkForwardConfig,
    WindowResult,
    aggregate_window_results,
    generate_windows,
)  # noqa: F401 - WalkForwardAggregate re-exported for callers
from tradingagents.marketdata.models import OhlcvSeries
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.research.logging import log_event


def slice_upto(dataset: OhlcvSeries, moment: datetime) -> OhlcvSeries:
    """Point-in-time view: bars with ``timestamp <= moment`` only."""
    stamps = [b.timestamp for b in dataset.bars]
    hi = bisect.bisect_right(stamps, moment)
    return OhlcvSeries(
        asset_id=dataset.asset_id,
        timeframe=dataset.timeframe,
        source=dataset.source,
        status=dataset.status,
        bars=dataset.bars[:hi],
    )


class _CountingLLM:
    """Delegating proxy counting every LLM invocation for cost tracking."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        result = self._inner.invoke(*args, **kwargs)
        usage = getattr(result, "usage_metadata", None)
        if isinstance(usage, dict):
            self.prompt_tokens += int(usage.get("input_tokens") or 0)
            self.completion_tokens += int(usage.get("output_tokens") or 0)
        return result


class AIResearchStrategy:
    """The Phase 1 research engine used as a backtest strategy.

    News and sentiment are disabled by default: their live-only endpoints
    cannot provide point-in-time historical content, so using them would
    fabricate recency (spec §"News and sentiment in backtests"). Macro
    (FRED) supports ``curr_date`` windows and can be enabled explicitly.
    """

    strategy_id = "ai_research"
    kind = "ai_research"

    def __init__(
        self,
        *,
        asset_id: str,
        timeframe: Timeframe,
        research_config: dict[str, Any],
        llm: Any = None,
        enable_macro: bool = False,
        research_cache: ResearchCache | None = None,
    ):
        self.asset_id = asset_id
        self.timeframe = timeframe
        self.research_config = dict(research_config)
        self.llm_input = llm
        self.enable_macro = enable_macro
        self.cache = research_cache
        self.usage_llm: Any | None = None
        self.research_runs = 0
        self.engine: Any = None  # bound after attach()

    # engine wiring -----------------------------------------------------------

    def attach(self, provider: ReplayMarketDataProvider) -> None:
        from tradingagents.research.engine import ResearchEngine

        llm = self.llm_input
        if llm is None:
            # Build the same default client the live engine would build, but
            # wrap it so every call is counted for the report's AIUsage.
            from tradingagents.llm_clients import create_llm_client

            client = create_llm_client(
                provider=self.research_config["llm_provider"],
                model=self.research_config["quick_think_llm"],
                base_url=self.research_config.get("backend_url"),
            )
            llm = client.get_llm()
        counter = _CountingLLM(llm)
        self.usage_llm = counter
        disabled = ["news", "sentiment"] + ([] if self.enable_macro else ["macro"])
        self.engine = ResearchEngine(
            config=self.research_config,
            provider=provider,
            llm=llm,
            now_fn=provider.clock.now,
            disabled_components=tuple(disabled),
        )

    @property
    def model_ids(self) -> list[str]:
        return [
            str(self.research_config.get("quick_think_llm", "unknown")),
            str(self.research_config.get("deep_think_llm", "unknown")),
        ]

    def config_hash(self) -> str:
        identity = f"{PROMPT_VERSION}|{sorted(self.research_config.items())}"
        return hashlib.sha256(identity.encode()).hexdigest()[:12]

    # strategy protocol ----------------------------------------------------------

    def generate(
        self, *, asset_id: str, timeframe: str, visible: OhlcvSeries, now: datetime
    ) -> Any | None:
        assert self.engine is not None, "attach() must be called before generate()"
        if self.cache is not None:
            cached = self.cache.get(
                asset_id=asset_id, timeframe=timeframe,
                decision_at=now.isoformat(), visible=visible,
                model_ids=self.model_ids, config_hash=self.config_hash(),
            )
            if cached is not None:
                return cached
        result = self.engine.run(asset_id, timeframe)
        self.research_runs += 1
        signal = result.signal
        if signal is not None and self.cache is not None:
            self.cache.put(
                signal,
                decision_at=now.isoformat(), visible=visible,
                model_ids=self.model_ids, config_hash=self.config_hash(),
            )
        return signal


@dataclass
class BacktestRunOutput:
    """Everything one ``run_backtest`` invocation produced, keyed by strategy."""

    results: dict[str, StrategyResult]
    ledgers: dict[str, TradeLedger] = field(default_factory=dict)
    window_shells: list[WindowResult] = field(default_factory=list)


def run_backtest(
    *,
    dataset: OhlcvSeries,
    strategies: list[Any],
    initial_capital: float = 10_000.0,
    execution_cfg: ExecutionConfig | None = None,
    sizing: SizingPolicy | None = None,
    limits: RiskLimits | None = None,
    warmup_bars: int = 210,
    decision_stride_bars: int = 1,
    decision_start_idx: int | None = None,
    run_id: str = "bt",
) -> BacktestRunOutput:
    """Run every strategy sequentially over the same dataset timeline.

    Determinism: identical inputs produce identical ledgers/equity curves;
    only the report's run id/creation time vary.
    """
    cfg = execution_cfg or ExecutionConfig()
    sizing = sizing or SizingPolicy()
    limits = limits or RiskLimits()
    if len(dataset.bars) <= warmup_bars + 2:
        raise ValueError(
            f"dataset too small: {len(dataset.bars)} bars "
            f"(need > warmup {warmup_bars} + 2)"
        )
    tf = dataset.timeframe
    first_decision = (
        decision_start_idx if decision_start_idx is not None else warmup_bars - 1
    )
    if first_decision < warmup_bars - 1:
        raise ValueError("decision_start_idx must allow indicator warm-up")

    results: dict[str, StrategyResult] = {}
    ledgers_by_strategy: dict[str, TradeLedger] = {}
    window_shells: list[WindowResult] = []

    for strategy in strategies:
        strategy_id = getattr(strategy, "strategy_id", strategy.__class__.__name__)
        # One independent clock per strategy: each pass replays the same
        # timeline from the start, and SimulationClock forbids rewinding.
        clock = SimulationClock(dataset.bars[0].timestamp)
        portfolio = Portfolio(initial_capital=initial_capital)
        ledger = TradeLedger(run_id=run_id)
        simulator = ExecutionSimulator(
            run_id=run_id, strategy_id=strategy_id,
            asset_id=dataset.asset_id, timeframe=tf.value,
            timeframe_minutes=tf.minutes, execution_cfg=cfg,
            sizing=sizing, limits=limits, portfolio=portfolio, ledger=ledger,
        )
        curve: list[EquityPoint] = []
        # Anchor the curve at the configured capital BEFORE any trade exists,
        # so returns/drawdowns measure from the true starting equity.
        curve.append(EquityPoint(
            timestamp=dataset.bars[first_decision].timestamp,
            equity=round(initial_capital, 10), cash=initial_capital,
            exposure=0.0, open_positions=0, drawdown_pct=0.0,
        ))

        # Bind AI strategies to this pass's replay provider/clock.
        if getattr(strategy, "kind", "") == "ai_research":
            provider = ReplayMarketDataProvider(dataset, clock)
            strategy.attach(provider)

        i = first_decision
        n = len(dataset.bars)
        while i < n:
            bar = dataset.bars[i]
            clock.set(bar.timestamp)
            visible = slice_upto(dataset, bar.timestamp)
            try:
                signal = strategy.generate(
                    asset_id=dataset.asset_id, timeframe=tf.value,
                    visible=visible, now=bar.timestamp,
                )
            except Exception as exc:  # noqa: BLE001 - strategy failure != bad data
                log_event(
                    "backtest_strategy_error", strategy_id=strategy_id,
                    error_type=type(exc).__name__,
                )
                signal = None
            if signal is not None:
                simulator.schedule_signal(signal, now=bar.timestamp)

            # Walk bars strictly after the decision close until the next
            # decision: resolve pending fills at opens, stops at closes.
            next_i = min(i + decision_stride_bars, n - 1)
            j = i + 1
            while j <= next_i:
                walk_bar = dataset.bars[j]
                simulator.on_bar_open(walk_bar)
                simulator.on_bar_close(walk_bar)
                mark = {dataset.asset_id: walk_bar.close}
                curve.append(EquityPoint(
                    timestamp=walk_bar.timestamp,
                    equity=round(portfolio.equity(mark), 10),
                    cash=portfolio.cash,
                    exposure=portfolio.gross_exposure(mark),
                    open_positions=len(portfolio.positions),
                    drawdown_pct=0.0,
                ))
                j += 1
            if next_i == n - 1:
                break
            i = next_i

        last_bar = dataset.bars[-1]
        if curve and curve[-1].timestamp < last_bar.timestamp:
            mark = {dataset.asset_id: last_bar.close}
            curve.append(EquityPoint(
                timestamp=last_bar.timestamp,
                equity=round(portfolio.equity(mark), 10),
                cash=portfolio.cash,
                exposure=portfolio.gross_exposure(mark),
                open_positions=len(portfolio.positions),
                drawdown_pct=0.0,
            ))
        simulator.force_close_last_bar(last_bar)
        final_mark = {dataset.asset_id: last_bar.close}
        curve.append(EquityPoint(
            timestamp=last_bar.timestamp,
            equity=round(portfolio.equity(final_mark), 10),
            cash=portfolio.cash,
            exposure=0.0,
            open_positions=0,
            drawdown_pct=0.0,
        ))

        stats: PerformanceStats = compute_stats(
            records=ledger.records, equity_curve=curve,
            timeframe_minutes=tf.minutes,
        )
        stats.benchmark_buy_hold_return_pct = buy_and_hold_reference(
            bars=dataset.bars[first_decision:], initial_capital=initial_capital,
        )
        kind = getattr(strategy, "kind", f"baseline:{strategy_id}")
        params = {
            k: v for k, v in vars(strategy).items()
            if isinstance(v, (int, float, str, bool)) and not k.startswith("_")
        }
        results[strategy_id] = StrategyResult(
            strategy_id=strategy_id, strategy_kind=kind,
            params=params, stats=stats, equity_curve=curve,
            trade_count=len(ledger.records),
        )
        ledgers_by_strategy[strategy_id] = ledger
        window_shells.append(_window_result_from(
            window_id=0, strategy_id=strategy_id, stats=stats,
            trades=len(ledger.records),
            start=curve[0].timestamp if curve else dataset.bars[0].timestamp,
            end=last_bar.timestamp,
        ))

    return BacktestRunOutput(
        results=results, ledgers=ledgers_by_strategy, window_shells=window_shells,
    )


def _window_result_from(
    *, window_id: int, strategy_id: str, stats: PerformanceStats,
    trades: int, start: datetime, end: datetime,
) -> WindowResult:
    return WindowResult(
        window_id=window_id, strategy_id=strategy_id,
        train_period=(start.isoformat(), end.isoformat()),
        test_period=(start.isoformat(), end.isoformat()),
        trades=trades,
        total_return_pct=stats.total_return_pct,
        max_drawdown_pct=stats.max_drawdown_pct,
        win_rate_pct=stats.win_rate_pct,
        profit_factor=stats.profit_factor,
        sharpe_ratio=stats.sharpe_ratio,
        sortino_ratio=stats.sortino_ratio,
    )


def build_report(
    *,
    dataset: OhlcvSeries,
    dataset_meta: Any,
    results: dict[str, StrategyResult],
    initial_capital: float,
    execution_cfg: ExecutionConfig,
    sizing: SizingPolicy,
    limits: RiskLimits,
    enabled_components: list[str],
    disabled_components: list[str],
    ai_strategy: AIResearchStrategy | None = None,
    run_id: str = "bt",
) -> BacktestReport:
    """Assemble the machine-readable BACKTEST_REPORT from real results."""
    report = BacktestReport(
        code_commit=git_commit(),
        asset_id=dataset.asset_id,
        timeframe=dataset.timeframe.value,
        period_start=dataset.bars[0].timestamp,
        period_end=dataset.bars[-1].timestamp,
        initial_capital=initial_capital,
        dataset_meta=dataset_meta,
        execution_assumptions=execution_cfg.model_dump(),
        sizing=sizing.model_dump(),
        risk_limits=limits.model_dump(),
        enabled_components=enabled_components,
        disabled_components=disabled_components,
        run_id=run_id,
    )
    report.strategies = list(results.values())
    if ai_strategy is not None:
        counter = ai_strategy.usage_llm
        report.ai_usage = AIUsage(
            enabled=True,
            model_ids=ai_strategy.model_ids,
            research_runs=ai_strategy.research_runs,
            llm_calls=counter.calls if counter else 0,
            prompt_tokens_est=counter.prompt_tokens if counter else None,
            completion_tokens_est=counter.completion_tokens if counter else None,
            estimated_cost_usd=None,  # never invent pricing
            cache_hits=ai_strategy.cache.hits if ai_strategy.cache else 0,
            cache_misses=ai_strategy.cache.misses if ai_strategy.cache else 0,
        )
    return report


def run_walk_forward(
    *,
    dataset: OhlcvSeries,
    strategies: list[Any],
    wf_config: WalkForwardConfig,
    initial_capital: float = 10_000.0,
    execution_cfg: ExecutionConfig | None = None,
    sizing: SizingPolicy | None = None,
    limits: RiskLimits | None = None,
) -> dict[str, tuple[list[WindowResult], WalkForwardAggregate]]:
    """Walk-forward over one dataset; per-window results stay independent.

    For every frame, strategy decisions are confined to the frame's test
    phase ([test_start, test_end)); earlier bars serve only as indicator
    context — structurally identical to live operation where a fresh engine
    sees full history up to now. Baselines here have no tunable parameters,
    so there is no in-sample fitting step; when parameterized strategies
    arrive, their fitting must happen inside the train/validation phases
    only. Aggregation happens per strategy after ALL windows complete.
    """
    cfg = execution_cfg or ExecutionConfig()
    sizing = sizing or SizingPolicy()
    limits = limits or RiskLimits()
    frames = generate_windows(len(dataset.bars), wf_config)
    by_strategy: dict[str, list[WindowResult]] = {}
    for frame in frames:
        for strategy in strategies:
            strategy_id = getattr(
                strategy, "strategy_id", strategy.__class__.__name__
            )
            # Out-of-sample run: decisions confined to [test_start, test_end).
            test_start = frame.validation_end  # WindowFrame has no test_start
            output = run_backtest(
                dataset=dataset,
                strategies=[strategy],
                initial_capital=initial_capital,
                execution_cfg=cfg, sizing=sizing, limits=limits,
                warmup_bars=test_start + 1,
                decision_stride_bars=1,
                decision_start_idx=test_start,
                run_id=f"wf{frame.window_id}",
            )
            result = output.results[strategy_id]
            window = WindowResult(
                window_id=frame.window_id,
                strategy_id=strategy_id,
                train_period=(
                    dataset.bars[frame.train_start].timestamp.isoformat(),
                    dataset.bars[frame.validation_end - 1].timestamp.isoformat(),
                ),
                validation_period=None,
                test_period=(
                    dataset.bars[test_start].timestamp.isoformat(),
                    dataset.bars[frame.test_end - 1].timestamp.isoformat(),
                ),
                trades=result.stats.n_trades,
                total_return_pct=result.stats.total_return_pct,
                max_drawdown_pct=result.stats.max_drawdown_pct,
                win_rate_pct=result.stats.win_rate_pct,
                profit_factor=result.stats.profit_factor,
                sharpe_ratio=result.stats.sharpe_ratio,
                sortino_ratio=result.stats.sortino_ratio,
            )
            by_strategy.setdefault(strategy_id, []).append(window)

    aggregated: dict[str, tuple[list[WindowResult], WalkForwardAggregate]] = {}
    for strategy_id, windows in by_strategy.items():
        aggregated[strategy_id] = (windows, aggregate_window_results(windows))
    return aggregated


__all__ = [
    "AIResearchStrategy",
    "BacktestRunOutput",
    "_CountingLLM",
    "build_report",
    "run_backtest",
    "run_walk_forward",
    "slice_upto",
]
