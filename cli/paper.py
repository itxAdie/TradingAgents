"""Paper-trading CLI commands.

Subcommands: init, run, status, report, halt, resume, note. All state lives
in the JSON paper store under ``{data_cache_dir}/paper/``; nothing here
touches brokers or real money (Phase 3 is simulation-only).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.paper.config import (
    ENV_TEST,
    PaperTradingConfig,
    ScheduleEntry,
)
from tradingagents.paper.engine import LiveResearchRunner, PaperTradingEngine
from tradingagents.paper.events import LoggingNotificationProvider
from tradingagents.paper.scheduler import ScheduleKey
from tradingagents.paper.store import JsonPaperStateStore, PaperStateError


def register_paper_command(app: typer.Typer) -> None:
    paper = typer.Typer(help="Paper trading (simulated execution only)")

    def _store(environment: str, account_id: str) -> JsonPaperStateStore:
        root = Path(str(DEFAULT_CONFIG["data_cache_dir"])) / "paper"
        return JsonPaperStateStore(root, environment=environment, account_id=account_id)

    def _engine(config: PaperTradingConfig) -> PaperTradingEngine:
        from tradingagents.marketdata.yahoo_provider import YahooMarketDataProvider

        return PaperTradingEngine(
            config=config,
            store=_store(config.environment, config.account_id),
            provider=YahooMarketDataProvider(),
            runner=LiveResearchRunner(
                provider=YahooMarketDataProvider(),
                now_fn=lambda: datetime.now(timezone.utc),
                disabled_components=("news", "sentiment"),
                enable_macro=config.enable_macro,
                research_config=config.research_config or None,
            ),
            notifier=LoggingNotificationProvider(),
        )

    def _config(
        environment: str,
        account: str,
        capital: float | None,
        enable: bool,
        assets: list[str],
        timeframes: list[str],
    ) -> PaperTradingConfig:
        schedules = [
            ScheduleEntry(asset_id=asset, timeframe=tf)
            for asset in assets
            for tf in timeframes
        ]
        kwargs: dict = {
            "environment": environment,
            "account_id": account,
            "enabled": enable,
            "schedules": schedules,
        }
        if capital is not None:
            kwargs["initial_capital"] = capital
        try:
            return PaperTradingConfig(**kwargs)
        except Exception as exc:
            typer.secho(f"Invalid configuration: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc

    @paper.command(name="init")
    def init_cmd(
        environment: Annotated[str, typer.Option("--env", help="test|paper")] = ENV_TEST,
        account: Annotated[str, typer.Option("--account")] = "paper-default",
        capital: Annotated[float, typer.Option("--capital", min=1.0)] = 10_000.0,
        enable: Annotated[bool, typer.Option("--enable/--no-enable")] = False,
        assets: Annotated[list[str] | None, typer.Option("--asset", "-a")] = None,
        timeframes: Annotated[
            list[str] | None, typer.Option("--timeframe", "-t")
        ] = None,
    ) -> None:
        """Create a virtual paper account (refuses to overwrite)."""
        asset_list = assets or ["XAUUSD"]
        tf_list = timeframes or ["1h"]
        config = _config(environment, account, capital, enable, asset_list, tf_list)
        engine = _engine(config)
        try:
            state = engine.init_account()
        except PaperStateError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
        typer.secho(
            f"Account {state.account_id} [{state.environment}] initialised with "
            f"{state.cash:,.2f}. Kill switch: {'ARMED' if config.enabled else 'OFF'}",
            fg=typer.colors.GREEN,
        )

    @paper.command(name="run")
    def run_cmd(
        environment: Annotated[str, typer.Option("--env")] = ENV_TEST,
        account: Annotated[str, typer.Option("--account")] = "paper-default",
        enable: Annotated[bool, typer.Option("--enable/--no-enable")] = True,
        assets: Annotated[list[str] | None, typer.Option("--asset", "-a")] = None,
        timeframes: Annotated[
            list[str] | None, typer.Option("--timeframe", "-t")
        ] = None,
        loop: Annotated[bool, typer.Option("--loop/--once")] = False,
    ) -> None:
        """Run due cycles once (or in a simple loop). Requires --enable."""
        asset_list = assets or ["XAUUSD"]
        tf_list = timeframes or ["1h"]
        config = _config(environment, account, None, enable, asset_list, tf_list)
        engine = _engine(config)
        if not loop:
            _run_once(engine, config, asset_list, tf_list)
            return
        typer.echo("Loop mode — Ctrl-C to stop.")
        while True:
            _run_once(engine, config, asset_list, tf_list, quiet_no_new_bar=True)
            try:
                _sleep_until_next(engine, config, asset_list, tf_list)
            except KeyboardInterrupt as exc:
                raise typer.Exit() from exc

    @paper.command(name="status")
    def status_cmd(
        environment: Annotated[str, typer.Option("--env")] = ENV_TEST,
        account: Annotated[str, typer.Option("--account")] = "paper-default",
    ) -> None:
        """Print the current account summary."""
        config = PaperTradingConfig(environment=environment, account_id=account)
        engine = _engine(config)
        try:
            engine.account_summary()
        except PaperStateError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
        report = engine.build_report()
        typer.echo(report.render_text())

    @paper.command(name="report")
    def report_cmd(
        environment: Annotated[str, typer.Option("--env")] = ENV_TEST,
        account: Annotated[str, typer.Option("--account")] = "paper-default",
        save: Annotated[Path | None, typer.Option("--save")] = None,
    ) -> None:
        """Render the structured account report (optionally save JSON)."""
        config = PaperTradingConfig(environment=environment, account_id=account)
        engine = _engine(config)
        report = engine.build_report()
        if save is not None:
            path = report.to_json(save)
            typer.echo(f"Saved: {path}")
        typer.echo(report.render_text())

    @paper.command(name="halt")
    def halt_cmd(
        environment: Annotated[str, typer.Option("--env")] = ENV_TEST,
        account: Annotated[str, typer.Option("--account")] = "paper-default",
        reason: Annotated[str, typer.Option("--reason")] = "manual emergency halt",
    ) -> None:
        """Emergency halt: no new trades until resume."""
        store = _store(environment, account)
        store.set_halted(True, reason)
        typer.secho(f"Halted: {reason}", fg=typer.colors.RED)

    @paper.command(name="resume")
    def resume_cmd(
        environment: Annotated[str, typer.Option("--env")] = ENV_TEST,
        account: Annotated[str, typer.Option("--account")] = "paper-default",
    ) -> None:
        """Clear the emergency halt."""
        store = _store(environment, account)
        store.set_halted(False)
        typer.secho("Halt cleared.", fg=typer.colors.GREEN)

    @paper.command(name="note")
    def note_cmd(
        trade_id: Annotated[str, typer.Option("--trade-id")],
        text: Annotated[str, typer.Option("--text")],
        environment: Annotated[str, typer.Option("--env")] = ENV_TEST,
        account: Annotated[str, typer.Option("--account")] = "paper-default",
    ) -> None:
        """Attach a human journal note to an existing trade."""
        from tradingagents.paper.models import JournalNote

        store = _store(environment, account)
        ok = store.add_journal_note(
            trade_id,
            JournalNote(timestamp=datetime.now(timezone.utc), text=text),
        )
        if ok:
            typer.secho("Note stored.", fg=typer.colors.GREEN)
        else:
            typer.secho(f"Trade {trade_id} has no journal entry.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    app.add_typer(paper, name="paper")


def _run_once(
    engine: PaperTradingEngine,
    config: PaperTradingConfig,
    assets: list[str],
    timeframes: list[str],
    *,
    quiet_no_new_bar: bool = False,
) -> None:
    for asset in assets:
        for tf_value in timeframes:
            result = engine.run_cycle(asset, tf_value)
            if result.status == "no_new_bar" and quiet_no_new_bar:
                continue
            typer.echo(f"[{asset} {tf_value}] {result.status}: {result.detail}")


def _sleep_until_next(
    engine: PaperTradingEngine,
    config: PaperTradingConfig,
    assets: list[str],
    timeframes: list[str],
) -> None:
    now = datetime.now(timezone.utc)
    delays: list[float] = []
    for asset in assets:
        for tf_value in timeframes:
            key = ScheduleKey(asset.upper(), Timeframe(tf_value.lower()).value).value()
            state = engine.store.load_schedule_state(key) or {}
            raw = state.get("next_run_at")
            if raw:
                next_run = datetime.fromisoformat(raw)
                if next_run > now:
                    delays.append((next_run - now).total_seconds())
    sleep_for = min(delays) if delays else 60.0
    time.sleep(max(5.0, min(sleep_for, 900.0)))
