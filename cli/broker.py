"""Broker/execution CLI commands (Phase 5 — sandbox only).

Subcommands: brokers, status, startup, shutdown, reconcile, halt, resume.
All state lives in the execution store under
``{data_cache_dir}/live/<environment>/<account>/``. The only registered
adapter is the deterministic sandbox; real venues are a future phase and
require the live activation ceremony (env flags + operator record).
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from cli.utils import console
from tradingagents.brokers.registry import available_brokers, build_broker
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution.config import load_live_execution_config
from tradingagents.execution.engine import LiveExecutionEngine
from tradingagents.marketdata.yahoo_provider import YahooMarketDataProvider
from tradingagents.paper.events import LoggingNotificationProvider


def register_broker_command(app: typer.Typer) -> None:
    broker = typer.Typer(help="Broker integration (sandbox adapter only)")

    def _engine(broker_name: str, account: str):
        try:
            config = load_live_execution_config(
                broker_name=broker_name,
                cache_dir=Path(str(DEFAULT_CONFIG["data_cache_dir"])),
                account_id=account or None,
            )
            adapter = build_broker(
                broker_name, account_id=config.account_id, base_currency=config.base_currency
            )
        except Exception as exc:
            typer.secho(f"Configuration refused: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc
        return LiveExecutionEngine(
            config=config,
            adapter=adapter,
            provider=YahooMarketDataProvider(),
            notifier=LoggingNotificationProvider(),
            audit=lambda action, **detail: None,
        )

    @broker.command(name="brokers")
    def brokers_cmd() -> None:
        """List registered broker adapters."""
        table = Table(title="Registered broker adapters")
        table.add_column("Name", style="cyan")
        for name in available_brokers():
            table.add_row(name)
        console.print(table)
        if available_brokers() == ["sandbox"]:
            console.print(
                "[dim]Sandbox only: real venue adapters arrive in a later phase "
                "after passing the contract suite.[/dim]"
            )

    @broker.command()
    def status(
        broker_name: Annotated[str, typer.Option("--broker")] = "sandbox",
        account: Annotated[str, typer.Option("--account")] = "",
    ) -> None:
        """Show execution environment status (no connection attempts)."""
        engine = _engine(broker_name, account)
        snap = engine.status_snapshot()
        halted = "[red]HALTED[/red]" if snap["halted"] else "[green]ok[/green]"
        tripped = (
            f"[red]TRIPPED ({snap['circuit_breaker_reason']})[/red]"
            if snap["circuit_breaker"]
            else "[green]ok[/green]"
        )
        recon = snap["last_reconciliation"]
        recon_txt = "-"
        if isinstance(recon, dict):
            recon_txt = (
                f"{recon.get('trigger')} @ {str(recon.get('ts'))[:19]} "
                f"({'clean' if recon.get('clean') else 'DIRTY'})"
            )
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column("Key", style="bold")
        table.add_column("Value")
        table.add_row("Broker", str(snap["broker"]))
        table.add_row("Environment", str(snap["environment"]))
        table.add_row("Account", str(snap["account_id"]))
        table.add_row("Connection", str(snap["connection"]))
        table.add_row("Halt", halted)
        table.add_row("Circuit breaker", tripped)
        table.add_row("Last reconciliation", recon_txt)
        table.add_row("Config version", str(snap["configuration_version"]))
        console.print(table)

    @broker.command()
    def startup(
        broker_name: Annotated[str, typer.Option("--broker")] = "sandbox",
        account: Annotated[str, typer.Option("--account")] = "",
    ) -> None:
        """Connect, verify account identity and reconcile (blocks on failure)."""
        engine = _engine(broker_name, account)
        ok, blockers = engine.startup()
        if ok:
            typer.secho("Execution engine ready.", fg=typer.colors.GREEN)
        else:
            typer.secho("Execution engine NOT ready:", fg=typer.colors.RED)
            for blocker in blockers:
                typer.echo(f"  - {blocker}")
            raise typer.Exit(code=1)

    @broker.command()
    def shutdown(
        broker_name: Annotated[str, typer.Option("--broker")] = "sandbox",
        account: Annotated[str, typer.Option("--account")] = "",
    ) -> None:
        """Final reconciliation pass + clean disconnect."""
        engine = _engine(broker_name, account)
        with contextlib.suppress(Exception):
            engine.adapter.connect()  # fresh process: reconnect for final pass
        summary = engine.shutdown()
        typer.echo(json.dumps(summary, indent=2, default=str))

    @broker.command()
    def reconcile(
        broker_name: Annotated[str, typer.Option("--broker")] = "sandbox",
        account: Annotated[str, typer.Option("--account")] = "",
    ) -> None:
        """Run one reconciliation pass against the broker (authoritative)."""
        engine = _engine(broker_name, account)
        try:
            status = engine.adapter.connect()
        except Exception as exc:
            typer.secho(f"Connect failed: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
        if status.value != "CONNECTED":
            typer.secho(f"Cannot reconcile while {status.value}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        report = engine.reconcile(trigger="manual")
        if report.clean:
            typer.secho(
                f"Reconciliation clean ({report.orders_checked} orders, "
                f"{report.positions_checked} positions).",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho(f"{len(report.mismatches)} mismatch(es):", fg=typer.colors.RED)
            for m in report.mismatches:
                typer.echo(f"  [{m.kind}] {m.detail}")
            raise typer.Exit(code=1)

    @broker.command()
    def halt(
        reason: str,
        broker_name: Annotated[str, typer.Option("--broker")] = "sandbox",
        account: Annotated[str, typer.Option("--account")] = "",
        operator: Annotated[str, typer.Option("--operator")] = "cli-operator",
    ) -> None:
        """Halt all new order flow (manual kill switch, persisted)."""
        engine = _engine(broker_name, account)
        engine.halt(reason, operator=operator)
        typer.secho(f"Halted: {reason}", fg=typer.colors.YELLOW)

    @broker.command()
    def resume(
        broker_name: Annotated[str, typer.Option("--broker")] = "sandbox",
        account: Annotated[str, typer.Option("--account")] = "",
        operator: Annotated[str, typer.Option("--operator")] = "cli-operator",
    ) -> None:
        """Manually resume after a halt / circuit-breaker trip."""
        engine = _engine(broker_name, account)
        engine.resume(operator=operator)
        tripped, _ = engine.store.circuit_breaker_state()
        if tripped:
            typer.secho("Breaker still tripped — inspect before resuming again.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        typer.secho("Resumed by operator.", fg=typer.colors.GREEN)

    app.add_typer(broker, name="broker")
