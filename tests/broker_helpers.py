"""Shared offline fixtures/helpers for broker/execution tests.

Same rules as paper_helpers: deterministic, scripted, no network, no LLMs,
no real venue. The sandbox adapter plays the broker; a fake provider plays
market data; a recording notifier captures every alert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tradingagents.brokers.sandbox import SandboxBrokerAdapter
from tradingagents.execution.config import load_live_execution_config
from tradingagents.execution.engine import LiveExecutionEngine
from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries
from tradingagents.research.schemas import (
    DataSourceRef,
    ResearchSignal,
    RiskLevel,
    SignalAction,
)

T0 = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)  # Monday 09:00 UTC


class SteppingClock:
    """Each call advances one second so event ordering stays monotonic."""

    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class FakeProvider:
    """One fresh hourly bar closing at ``bar_close`` (fresh by default)."""

    name = "fake"

    def __init__(self, bar_close: datetime):
        self.bar_close = bar_close

    def get_ohlcv(self, asset, timeframe, *, limit=None, start=None, end=None):
        return OhlcvSeries(
            asset_id=asset.asset_id,
            timeframe=timeframe,
            source="fake",
            status=DataStatus.SIMULATED,
            bars=[
                Bar(
                    timestamp=self.bar_close,
                    open=49_900.0,
                    high=50_100.0,
                    low=49_800.0,
                    close=50_000.0,
                )
            ],
        )

    def get_quote(self, asset):
        return None


class RecordingNotifier:
    """NotificationProvider double capturing every alert for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def notify(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def kinds(self) -> set[str]:
        return {event for event, _ in self.events}


def make_signal(
    *,
    action: SignalAction = SignalAction.BUY,
    entry: float = 50_000.0,
    stop: float | None = 49_000.0,
    target: float | None = 52_000.0,
    generated_at: datetime | None = None,
    asset_id: str = "BTCUSD",
) -> ResearchSignal:
    return ResearchSignal(
        asset_id=asset_id,
        generated_at=generated_at or T0,
        timeframe="1h",
        action=action,
        confidence=0.8,
        entry_reference=entry if action is not SignalAction.HOLD else None,
        stop_loss_reference=stop,
        take_profit_reference=target,
        risk_level=RiskLevel.MEDIUM,
        thesis="deterministic smoke decision",
        data_sources=[
            DataSourceRef(name="fake", kind="market_data", status="simulated")
        ],
        models_used=["test-model"],
    )


def build_engine(
    tmp_path: Path,
    *,
    account_id: str = "sbx-test",
    starting_cash: float = 100_000.0,
    clock: SteppingClock | None = None,
    provider: FakeProvider | None = None,
    limits_overrides: dict[str, float] | None = None,
    adapter: SandboxBrokerAdapter | None = None,
):
    """Config + sandbox adapter + engine wired over an isolated store root.

    Pass ``adapter=`` to reuse a venue across engine restarts (the sandbox
    book lives in memory, so a fresh instance would legitimately look empty).
    """
    from tradingagents.execution.config import LiveRiskLimits

    clock = clock or SteppingClock()
    limits = LiveRiskLimits(**(limits_overrides or {}))
    config = load_live_execution_config(
        broker_name="sandbox",
        cache_dir=tmp_path / "cache",
        account_id=account_id,
    ).model_copy(update={"limits": limits})
    adapter = adapter or SandboxBrokerAdapter(
        account_id=config.account_id,
        base_currency="USD",
        starting_cash=starting_cash,
        leverage_cap=20.0,
        clock=clock,
    )
    notifier = RecordingNotifier()
    engine = LiveExecutionEngine(
        config=config,
        adapter=adapter,
        provider=provider or FakeProvider(bar_close=clock.now - timedelta(hours=1)),
        notifier=notifier,
        bus=None,
        audit=lambda action, **detail: None,
        clock=clock,
    )
    return engine, adapter, notifier, clock
