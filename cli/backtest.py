"""Non-interactive ``backtest`` subcommand (Phase 2).

Runs deterministic baseline strategies by default; AI research backtests
require an explicit ``--ai`` flag because every decision step costs LLM
calls (spec §"Performance and safety requirements": never spend API money
without explicit approval). Reads historical datasets from the Phase 2
JSON store under the existing cache dir.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from tradingagents.backtest.baselines import (
    BuyAndHoldStrategy,
    MomentumStrategy,
    SmaCrossStrategy,
)
from tradingagents.backtest.config import ExecutionConfig, RiskLimits, SizingPolicy
from tradingagents.backtest.engine import build_report, run_backtest
from tradingagents.backtest.historical.store import JsonDataStore
from tradingagents.default_config import DEFAULT_CONFIG

console = Console()


def register_backtest_command(app: typer.Typer) -> None:
    @app.command(name="backtest")
    def backtest(  # noqa: C901 - CLI option wiring is linear by nature
        asset: str = typer.Option(
            ..., "--asset", "-a", help="Registered asset id, e.g. XAUUSD or BTCUSD."
        ),
        timeframe: str = typer.Option(
            "1h", "--timeframe", "-t", help="One of: 15m, 1h, 4h, 1d."
        ),
        start: str = typer.Option(
            ..., "--start", help="Backtest period start (YYYY-MM-DD UTC)."
        ),
        end: str = typer.Option(
            ..., "--end", help="Backtest period end (YYYY-MM-DD UTC, inclusive)."
        ),
        capital: float = typer.Option(
            10_000.0, "--capital", min=1.0, help="Starting capital."
        ),
        slippage_bps: float = typer.Option(
            1.0, "--slippage-bps", min=0.0, help="Adverse slippage per fill (bps)."
        ),
        spread_bps: float = typer.Option(
            2.0, "--spread-bps", min=0.0, help="Full quoted spread (bps)."
        ),
        commission_bps: float = typer.Option(
            0.5, "--commission-bps", min=0.0, help="Commission per side (bps of notional)."
        ),
        sizing_mode: str = typer.Option(
            "pct_equity",
            "--sizing",
            help="Position sizing: fixed_notional | fixed_quantity | pct_equity.",
        ),
        sizing_value: float = typer.Option(
            0.95, "--sizing-value", help="Meaning depends on --sizing mode."
        ),
        warmup_bars: int = typer.Option(
            210, "--warmup", min=10, help="Bars before the first decision (indicator history)."
        ),
        skip_ai: bool = typer.Option(
            True, "--skip-ai/--ai",
            help="Baselines only by default; --ai enables LLM research runs (COSTS MONEY).",
        ),
        fetch: bool = typer.Option(
            False, "--fetch/--no-fetch",
            help="Fetch missing data from Yahoo first (requires network).",
        ),
        save: Annotated[
            Path | None,
            typer.Option("--save", help="Directory for JSON report + CSV trade ledger."),
        ] = None,
    ) -> None:
        """Run a RESEARCH-ONLY historical simulation over stored datasets."""
        from tradingagents.backtest.historical.yahoo_history import fetch_and_store

        asset_spec = _resolve_asset(asset)
        tf = _resolve_timeframe(timeframe)
        start_dt = _parse_date(start)
        end_dt = _parse_date(end)

        cache_dir = Path(str(DEFAULT_CONFIG.get("data_cache_dir", ".cache")))
        store = JsonDataStore(cache_dir / "historical")

        try:
            dataset, meta = store.load(asset_spec.asset_id, tf)
            covered = (
                dataset.bars[0].timestamp <= start_dt
                and dataset.bars[-1].timestamp >= end_dt
            )
            if not covered:
                if not fetch:
                    raise typer.BadParameter(
                        f"stored window {dataset.bars[0]:%Y-%m-%d}.."
                        f"{dataset.bars[-1]:%Y-%m-%d} does not cover request; "
                        "re-run with --fetch"
                    )
                dataset, meta = fetch_and_store(
                    asset_spec, tf, start=start_dt, end=end_dt, store=store,
                )
        except FileNotFoundError:
            if not fetch:
                raise typer.BadParameter(
                    f"no stored dataset for {asset_spec.asset_id} @ {tf.value}; "
                    "run once with --fetch to download it from Yahoo"
                ) from None
            dataset, meta = fetch_and_store(
                asset_spec, tf, start=start_dt, end=end_dt, store=store,
            )

        strategies: list[object] = [
            BuyAndHoldStrategy(),
            SmaCrossStrategy(),
            MomentumStrategy(),
        ]

        ai_strategy = None
        enabled_components = ["market_data"]
        disabled_components = ["news", "sentiment", "macro", "ai_research"]
        if not skip_ai:
            from tradingagents.backtest.engine import AIResearchStrategy
            from tradingagents.backtest.research_cache import ResearchCache

            research_config = dict(DEFAULT_CONFIG)
            ai_strategy = AIResearchStrategy(
                asset_id=asset_spec.asset_id,
                timeframe=tf,
                research_config=research_config,
                enable_macro=False,
                research_cache=ResearchCache(cache_dir / "research_cache"),
            )
            strategies.append(ai_strategy)
            enabled_components = ["market_data", "ai_research"]
            disabled_components = ["news", "sentiment", "macro"]

        exec_cfg = ExecutionConfig(
            slippage_bps=slippage_bps, spread_bps=spread_bps,
            commission_per_side_bps=commission_bps,
        )
        sizing = SizingPolicy(mode=sizing_mode, value=sizing_value)
        limits = RiskLimits()
        output = run_backtest(
            dataset=dataset,
            strategies=strategies,
            initial_capital=capital,
            execution_cfg=exec_cfg,
            sizing=sizing,
            limits=limits,
            warmup_bars=min(warmup_bars, max(10, len(dataset.bars) - 5)),
        )
        report = build_report(
            dataset=dataset, dataset_meta=meta, results=output.results,
            initial_capital=capital,
            execution_cfg=exec_cfg, sizing=sizing, limits=limits,
            enabled_components=enabled_components,
            disabled_components=disabled_components,
            ai_strategy=ai_strategy,
            run_id=f"bt-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}",
        )

        console.print(Panel(report.render_text(), title="TradingAgents Backtester"))
        if save is not None:
            save.mkdir(parents=True, exist_ok=True)
            report_path = report.to_json(save / f"backtest_report_{report.run_id}.json")
            console.print(f"[green]Saved report:[/green] {report_path}")
            for strategy_id, ledger in output.ledgers.items():
                csv_name = f"trades_{strategy_id}_{report.run_id}.csv"
                ledger.to_csv(save / csv_name)
                ledger.to_json(save / csv_name.replace(".csv", ".json"))
                console.print(f"[green]Saved ledger:[/green] {save / csv_name}")


def _resolve_asset(asset_id: str):
    from tradingagents.assets.registry import get_asset

    return get_asset(asset_id.strip().upper())


def _resolve_timeframe(value: str):
    from tradingagents.marketdata.timeframes import Timeframe

    return Timeframe(value.strip().lower())


def _parse_date(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise typer.BadParameter(f"invalid date {value!r}: use YYYY-MM-DD") from exc
    return parsed.replace(tzinfo=timezone.utc)


__all__ = ["register_backtest_command"]
