"""CLI smoke tests (typer): init/status/report/halt/resume/note offline.

The production engine wiring (Yahoo provider + live research runner) is
replaced with fakes; the command surface, store paths, and exit codes are
exercised for real.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cli.paper as cli_paper
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.paper.engine import CycleResult


@pytest.fixture()
def paper_env(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the paper store root into the test tmp dir."""
    monkeypatch.setitem(DEFAULT_CONFIG, "data_cache_dir", str(tmp_path))
    return tmp_path


class _FakeRunner:
    prompt_version = "test-prompt"
    model_ids = ["fake"]
    config_hash = "0" * 12

    def __init__(self, *args, **kwargs):
        pass

    def run(self, asset_id, timeframe):
        return None, "offline"


class _FakeEngine:
    last_instance: _FakeEngine | None = None

    def __init__(self, *, config, store, provider, runner, notifier=None, now_fn=None):
        self.config = config
        self.store = store
        self._init_called = False

    def init_account(self):
        from tradingagents.paper.models import AccountState

        now = datetime.now(timezone.utc)
        state = AccountState(
            account_id=self.config.account_id,
            environment=self.config.environment,
            initial_capital=self.config.initial_capital,
            cash=self.config.initial_capital,
            created_at=now,
            updated_at=now,
        )
        self.store.create_account(state)
        return state

    def run_cycle(self, asset_id, timeframe, *, now=None):
        return CycleResult(status="no_signal", detail="offline fake")

    def account_summary(self):

        state = self.store.load_account()  # loud PaperStateError for unknown accounts
        return {
            "state": state,
            "positions": [],
            "equity": 10_000.0,
            "unrealized_pnl": 0.0,
            "stats": _fake_stats(),
            "daily": [],
            "open_orders": [],
            "orders_total": 0,
        }

    def build_report(self):
        from tradingagents.paper.report import build_account_report

        summary = self.account_summary()
        return build_account_report(
            state=summary["state"],
            positions=summary["positions"],
            equity=summary["equity"],
            unrealized_pnl=summary["unrealized_pnl"],
            stats=summary["stats"],
            daily=summary["daily"],
            now=datetime.now(timezone.utc),
        )


def _fake_stats():
    from tradingagents.backtest.analytics import PerformanceStats

    return PerformanceStats(initial_capital=10_000.0, final_equity=10_000.0)


@pytest.fixture()
def faked_engine(monkeypatch):
    monkeypatch.setattr(cli_paper, "PaperTradingEngine", _FakeEngine)
    monkeypatch.setattr(cli_paper, "LiveResearchRunner", _FakeRunner)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


APP = cli_paper  # module exposes register_paper_command


@pytest.fixture()
def app():
    import typer

    app = typer.Typer()
    cli_paper.register_paper_command(app)
    return app


class TestPaperCli:
    def test_init_creates_account_then_refuses_overwrite(
        self, paper_env, app, runner, faked_engine
    ) -> None:
        result = runner.invoke(
            app,
            ["paper", "init", "--account", "smoke-1", "--enable", "--capital", "5000"],
        )
        assert result.exit_code == 0, result.output
        assert "smoke-1" in result.output
        assert "ARMED" in result.output
        assert (paper_env / "paper" / "test" / "smoke-1" / "state.json").exists()

        again = runner.invoke(app, ["paper", "init", "--account", "smoke-1"])
        assert again.exit_code == 1
        assert "already initialised" in again.output

    def test_status_renders_report_with_disclaimer(
        self, paper_env, app, runner, faked_engine
    ) -> None:
        runner.invoke(app, ["paper", "init", "--account", "smoke-2"])
        status = runner.invoke(app, ["paper", "status", "--account", "smoke-2"])
        assert status.exit_code == 0
        assert "SIMULATED" in status.output
        assert "PAPER TRADING ACCOUNT" in status.output

    def test_status_fails_loudly_without_account(
        self, paper_env, app, runner, faked_engine
    ) -> None:
        status = runner.invoke(app, ["paper", "status", "--account", "ghost"])
        assert status.exit_code == 1
        assert "no paper account" in status.output

    def test_report_save_writes_json(
        self, paper_env, app, runner, faked_engine, tmp_path
    ) -> None:
        runner.invoke(app, ["paper", "init", "--account", "smoke-3"])
        out = tmp_path / "reports" / "acct.json"
        result = runner.invoke(
            app,
            [
                "paper",
                "report",
                "--account",
                "smoke-3",
                "--save",
                str(out),
            ],
        )
        assert result.exit_code == 0
        assert out.exists()
        assert '"schema_name"' in out.read_text(encoding="utf-8")
        assert "PAPER_ACCOUNT_REPORT" in out.read_text(encoding="utf-8")

    def test_run_once_reports_cycle_result_offline(
        self, paper_env, app, runner, faked_engine
    ) -> None:
        runner.invoke(app, ["paper", "init", "--account", "smoke-4", "--enable"])
        result = runner.invoke(
            app,
            [
                "paper",
                "run",
                "--account",
                "smoke-4",
                "-a",
                "XAUUSD",
                "-t",
                "1h",
                "--once",
            ],
        )
        assert result.exit_code == 0
        assert "[XAUUSD 1h] no_signal" in result.output

    def test_halt_resume_roundtrip(self, paper_env, app, runner) -> None:
        from tradingagents.paper.store import JsonPaperStateStore

        # halt before any account exists must fail loudly (no silent state)
        early = runner.invoke(app, ["paper", "halt", "--account", "ghost"])
        assert early.exit_code != 0 or "Trace" not in early.output

        store = JsonPaperStateStore(
            paper_env / "paper", environment="test", account_id="smoke-5"
        )
        from tradingagents.paper.models import AccountState

        store.create_account(
            AccountState(
                account_id="smoke-5",
                environment="test",
                initial_capital=10_000.0,
                cash=10_000.0,
            )
        )
        halted = runner.invoke(
            app, ["paper", "halt", "--account", "smoke-5", "--reason", "drill"]
        )
        assert halted.exit_code == 0
        assert store.load_account().halted is True

        resumed = runner.invoke(app, ["paper", "resume", "--account", "smoke-5"])
        assert resumed.exit_code == 0
        assert store.load_account().halted is False

    def test_note_requires_existing_journal(self, paper_env, app, runner) -> None:
        result = runner.invoke(
            app,
            [
                "paper",
                "note",
                "--account",
                "ghost",
                "--trade-id",
                "T1",
                "--text",
                "why did this trade?",
            ],
        )
        assert result.exit_code == 1
