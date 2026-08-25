"""``serve`` command: run the Phase 4 web terminal (API + dashboard).

The server is read-mostly; the only mutations are audited backtest starts,
journal notes, and (explicitly) arming the research loop. No trade-execution
endpoint exists — the dashboard can never place orders, paper or otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from tradingagents.api.app import ServerSettings, create_app

_DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "dashboard" / "dist"


def register_api_command(app: typer.Typer) -> None:
    @app.command(name="serve")
    def serve(
        host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
        port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
        environment: Annotated[str, typer.Option("--env")] = "test",
        account: Annotated[str, typer.Option("--account")] = "paper-default",
        assets: Annotated[list[str] | None, typer.Option("--asset", "-a")] = None,
        timeframes: Annotated[list[str] | None, typer.Option("--timeframe", "-t")] = None,
        enable_loop: Annotated[
            bool,
            typer.Option(
                "--enable-research-loop/--no-enable-research-loop",
                help="ARM THE KILL SWITCH and run live research cycles from the "
                "server (costs LLM money). Default off.",
            ),
        ] = False,
        quote_poll_seconds: Annotated[int, typer.Option("--quote-poll-seconds")] = 30,
        dashboard_dir: Annotated[
            Path | None,
            typer.Option("--dashboard-dir", help="Built frontend directory."),
        ] = None,
    ) -> None:
        """Run the web-terminal API server (PAPER TRADING - SIMULATED ONLY)."""
        settings = ServerSettings(
            environment=environment,
            account_id=account,
            assets=[a.upper() for a in (assets or ["XAUUSD", "BTCUSD"])],
            timeframes=[t.lower() for t in (timeframes or ["15m", "1h", "4h", "1d"])],
            enable_research_loop=enable_loop,
            quote_poll_seconds=max(0, quote_poll_seconds),
            dashboard_dir=(dashboard_dir or _DASHBOARD_DIST) if (
                dashboard_dir or _DASHBOARD_DIST.exists()
            ) else None,
            api_token=os.environ.get("TRADINGAGENTS_API_TOKEN") or None,
        )
        import uvicorn

        typer.secho(
            f"Serving TradingAgents web terminal on http://{host}:{port} "
            f"[{settings.environment}/{settings.account_id}] "
            f"(research loop: {'ARMED' if enable_loop else 'off'})",
            fg=typer.colors.YELLOW if enable_loop else typer.colors.GREEN,
        )
        typer.echo(
            "PAPER TRADING - SIMULATED EXECUTION ONLY. Broker integration is "
            "sandbox-adapter only (no real venue connectivity)."
        )
        uvicorn.run(create_app(settings), host=host, port=port, log_level="info")

    # Registration convention: flat command alongside research/backtest/paper.


__all__ = ["register_api_command"]
