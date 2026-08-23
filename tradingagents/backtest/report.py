"""Structured backtest report + run provenance.

Every report embeds: a unique run id, the git commit of the code that ran,
full execution assumptions (costs/sizing/limits echo), dataset provenance
ids, AI usage/cost tracking, which research components were enabled, and the
mandatory simulation-vs-reality disclaimer. Values come only from the
simulation — never invented.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from tradingagents.backtest.analytics import EquityPoint, PerformanceStats
from tradingagents.backtest.historical.store import DatasetMeta

DISCLAIMER = (
    "HISTORICAL SIMULATION ONLY - past simulated performance does not imply "
    "or guarantee any future result. RESEARCH SIGNAL - NOT EXECUTED."
)


def git_commit() -> str:
    """Best-effort HEAD sha; 'unknown' outside a repo / without git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 - provenance is best-effort by design
        return "unknown"


class AIUsage(BaseModel):
    """LLM usage for one backtest run; costs only when pricing is configured."""

    enabled: bool = False
    model_ids: list[str] = Field(default_factory=list)
    research_runs: int = 0
    llm_calls: int = 0
    prompt_tokens_est: int | None = None
    completion_tokens_est: int | None = None
    estimated_cost_usd: float | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    pricing_note: str = (
        "no provider pricing configured; usage reported without cost "
        "(never invent prices)"
    )


class StrategyResult(BaseModel):
    """Outcome of one strategy over one (asset, timeframe) window."""

    strategy_id: str
    strategy_kind: str  # "ai_research" | "baseline_*"
    params: dict[str, object] = Field(default_factory=dict)
    stats: PerformanceStats
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    trade_count: int = 0


class BacktestReport(BaseModel):
    """Machine-readable BACKTEST REPORT (JSON-first)."""

    schema_name: str = "BACKTEST_REPORT"
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    code_commit: str = ""
    asset_id: str
    timeframe: str
    period_start: datetime
    period_end: datetime
    initial_capital: float
    dataset_meta: DatasetMeta
    execution_assumptions: dict[str, object] = Field(default_factory=dict)
    sizing: dict[str, object] = Field(default_factory=dict)
    risk_limits: dict[str, object] = Field(default_factory=dict)
    enabled_components: list[str] = Field(default_factory=list)
    disabled_components: list[str] = Field(default_factory=list)
    ai_usage: AIUsage = Field(default_factory=AIUsage)
    strategies: list[StrategyResult] = Field(default_factory=list)
    walk_forward: list[dict[str, object]] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def config_hash(self) -> str:
        """Hash over every reproducibility-relevant field except ids/time."""
        identity = self.model_dump_json(
            exclude={"run_id", "created_at", "code_commit"}
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]

    def to_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(json.loads(self.model_dump_json()), indent=1),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _fmt(value: float | None, suffix: str = "") -> str:
        if value is None:
            return "N/A"
        if isinstance(value, float):
            return f"{value:.2f}{suffix}"
        return f"{value}{suffix}"

    def render_text(self) -> str:
        """Human-readable console rendering (values straight from stats)."""
        lines = ["", "BACKTEST REPORT", "=" * 60]
        lines.append(f"Run {self.run_id} | commit {self.code_commit[:8]}")
        lines.append(f"Asset: {self.asset_id}   Timeframe: {self.timeframe}")
        lines.append(
            f"Period: {self.period_start:%Y-%m-%d %H:%M} to "
            f"{self.period_end:%Y-%m-%d %H:%M} UTC"
        )
        lines.append(f"Initial capital: ${self.initial_capital:,.2f}")
        meta = self.dataset_meta
        lines.append(
            f"Dataset: {meta.dataset_id} ({meta.bar_count} bars, "
            f"source={meta.source}, fetched_at={meta.fetched_at:%Y-%m-%d %H:%M})"
        )
        if self.disabled_components:
            lines.append(
                "Research components DISABLED: "
                f"{', '.join(self.disabled_components)} "
                "(unavailable for historical simulation; never fabricated)"
            )
        if self.ai_usage.enabled:
            au = self.ai_usage
            lines.append(
                f"AI usage: {au.research_runs} research runs, {au.llm_calls} LLM calls, "
                f"cache hits {au.cache_hits}/{au.cache_hits + au.cache_misses}"
            )
        for s in self.strategies:
            st = s.stats
            lines += ["-" * 60, f"Strategy: {s.strategy_id}"]
            lines.append(f"Final equity:   ${st.final_equity:,.2f}")
            lines.append(f"Total return:   {self._fmt(st.total_return_pct, '%')}")
            bh = st.benchmark_buy_hold_return_pct
            lines.append(
                f"vs Buy&Hold:    {self._fmt(bh, '%')} "
                + (
                    f"(strategy {'OUTPERFORMS' if st.total_return_pct > bh else 'UNDERPERFORMS' if st.total_return_pct < bh else 'MATCHES'} benchmark by {st.total_return_pct - bh:+.2f}pp)"
                    if bh is not None and st.total_return_pct is not None else ""
                )
            )
            lines.append(f"Max drawdown:   {self._fmt(st.max_drawdown_pct, '%')}")
            lines.append(
                f"Trades:         {st.n_trades} "
                f"(win rate {self._fmt(st.win_rate_pct, '%')})"
            )
            lines.append(f"Profit factor:  {self._fmt(st.profit_factor)}")
            lines.append(f"Sharpe:         {self._fmt(st.sharpe_ratio)}")
            lines.append(f"Sortino:        {self._fmt(st.sortino_ratio)}")
            lines.append(
                f"Avg trade PnL:  ${st.average_trade_pnl:,.2f}"
                if st.average_trade_pnl is not None else "Avg trade PnL:  N/A"
            )
            if st.na_reasons:
                lines.append(
                    "N/A notes: "
                    + "; ".join(f"{k}: {v}" for k, v in sorted(st.na_reasons.items()))
                )
        lines += ["=" * 60, self.disclaimer]
        return "\n".join(lines)


__all__ = [
    "AIUsage",
    "BacktestReport",
    "DISCLAIMER",
    "StrategyResult",
    "git_commit",
]
