"""Non-interactive `research` subcommand (Phase 1 research platform).

Runs the research-only engine for a registered asset/timeframe and prints the
labelled research signal. This command never executes trades and shares no
state with the interactive `analyze` flow beyond configuration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.research.engine import ResearchEngine
from tradingagents.research.logging import log_event
from tradingagents.research.schemas import render_signal

console = Console()


def _save_artifacts(save_dir: Path, report_json: str, signal_json: str | None) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / f"research_report_{stamp}.json").write_text(report_json, encoding="utf-8")
    if signal_json is not None:
        (save_dir / f"research_signal_{stamp}.json").write_text(signal_json, encoding="utf-8")


def register_research_command(app: typer.Typer) -> None:
    @app.command(name="research")
    def research(
        asset: str = typer.Option(
            ..., "--asset", "-a", help="Registered asset id, e.g. XAUUSD or BTCUSD."
        ),
        timeframe: str = typer.Option(
            "1h", "--timeframe", "-t", help="One of: 15m, 1h, 4h, 1d."
        ),
        save: Annotated[
            Path | None,
            typer.Option("--save", help="Directory for JSON report/signal artifacts."),
        ] = None,
    ) -> None:
        """Run research-only analysis; output is a labelled RESEARCH SIGNAL."""
        log_event(
            "cli_restart", event_note="research command invoked",
            asset=asset, timeframe=timeframe,
        )
        engine = ResearchEngine(config=DEFAULT_CONFIG.copy())
        try:
            result = engine.run(asset, timeframe)
        except Exception as exc:
            console.print(Panel(
                f"[red]Research run failed:[/red] {exc}\n"
                "No signal was produced.",
                title="Research error", border_style="red",
            ))
            raise typer.Exit(code=1) from None

        if result.signal is not None:
            console.print(Panel(
                render_signal(result.signal),
                title=f"{result.report.display_name} — {result.report.timeframe}",
                border_style="cyan",
            ))
        else:
            reasons = [f.agent for f in result.report.agent_failures] or ["unknown"]
            console.print(Panel(
                "No research signal was emitted.\n"
                f"Reason: {result.no_signal_reason or 'sections unavailable'}\n"
                f"Affected stages: {', '.join(sorted(set(reasons)))}",
                title=f"{result.report.display_name} — no signal",
                border_style="yellow",
            ))

        if save is not None:
            _save_artifacts(
                save,
                result.report.model_dump_json(indent=2),
                result.signal.model_dump_json(indent=2) if result.signal else None,
            )
            console.print(f"[green]Artifacts saved under[/green] {save}")

        # Always surface the disclaimer outside the panel too.
        console.print("[bold]RESEARCH SIGNAL — NOT EXECUTED[/bold]")
