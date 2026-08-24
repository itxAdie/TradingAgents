"""Shared offline fixtures/helpers for paper-trading tests.

Everything here is deterministic: scripted SIMULATED-status providers,
canned research runners, a controllable clock. No network, no LLMs,
no broker code — per PROJECT_RULES §26 every provider response carries
``status=SIMULATED``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tradingagents.backtest.config import SizingPolicy
from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries
from tradingagents.paper.config import (
    PaperRiskLimits,
    PaperTradingConfig,
    ScheduleEntry,
)
from tradingagents.paper.engine import PaperTradingEngine
from tradingagents.paper.store import JsonPaperStateStore
from tradingagents.research.schemas import (
    DataSourceRef,
    ResearchSignal,
    RiskLevel,
    SignalAction,
)

T0 = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)  # Monday 09:00 UTC


class Clock:
    """Controllable clock standing in for wall time."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


def make_bar(
    stamp: datetime, *, op: float, hi: float, lo: float, cl: float, v: float = 100.0
) -> Bar:
    return Bar(timestamp=stamp, open=op, high=hi, low=lo, close=cl, volume=v)


def hourly(stamp: datetime, **levels: float) -> Bar:
    return make_bar(stamp, **levels)


def scenario_bars() -> list[Bar]:
    """Hourly XAUUSD bars.

    B0..B2 warm-up; B3 (12:00) is the first decision bar at clock 13:05;
    B4 (13:00) fills pending intents at its open; B5 (14:00) crashes through
    the default stop; B6 recovers.
    """
    h = lambda n: T0 + timedelta(hours=n)  # noqa: E731
    return [
        hourly(h(0), op=1998.0, hi=2002.0, lo=1995.0, cl=2000.0),
        hourly(h(1), op=2000.0, hi=2006.0, lo=1999.0, cl=2005.0),
        hourly(h(2), op=2005.0, hi=2012.0, lo=2003.0, cl=2010.0),
        hourly(h(3), op=2010.0, hi=2016.0, lo=2008.0, cl=2015.0),  # decision bar 1
        hourly(h(4), op=2016.0, hi=2021.0, lo=2013.0, cl=2020.0),  # fill bar
        hourly(h(5), op=2018.0, hi=2019.0, lo=1900.0, cl=1910.0),  # stop breach
        hourly(h(6), op=1912.0, hi=1930.0, lo=1905.0, cl=1925.0),
    ]


class ScriptedProvider:
    """Returns one fixed bar series labelled SIMULATED."""

    name = "scripted"

    def __init__(self, bars: list[Bar]):
        self.bars = list(bars)
        self.fail_with: Exception | None = None

    def get_ohlcv(
        self,
        asset: Any,
        timeframe: Any,
        *,
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> OhlcvSeries:
        if self.fail_with is not None:
            raise self.fail_with
        series = OhlcvSeries(
            asset_id=getattr(asset, "asset_id", str(asset)),
            timeframe=timeframe,
            source="scripted",
            status=DataStatus.SIMULATED,
            bars=list(self.bars),
        )
        if limit is not None:
            series = series.model_copy(update={"bars": series.bars[-limit:]})
        return series

    def get_quote(self, asset: Any) -> None:
        return None


class FakeRunner:
    """Deterministic ResearchRunner stand-in (no LLM, no network)."""

    prompt_version = "test-prompt-v1"
    model_ids = ["fake-quick", "fake-deep"]
    config_hash = "0123456789ab"

    def __init__(
        self,
        signal: ResearchSignal | None = None,
        no_signal_reason: str = "",
    ):
        self.canned_signal = signal
        self.no_signal_reason = no_signal_reason
        self.calls = 0
        self.fail_with: Exception | None = None

    def run(self, asset_id: str, timeframe: str) -> tuple[ResearchSignal | None, str]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.canned_signal, self.no_signal_reason


# -- canned research signals -------------------------------------------------


def make_signal(
    *,
    action: SignalAction = SignalAction.BUY,
    price: float = 2015.0,
    generated_at: datetime,
    stop_distance_pct: float = 0.03,
    target_distance_pct: float = 0.06,
    confidence: float = 0.7,
    asset_id: str = "XAUUSD",
    timeframe: str = "1h",
) -> ResearchSignal:
    directional = action in (SignalAction.BUY, SignalAction.SELL)
    side = 1 if action is SignalAction.BUY else -1
    return ResearchSignal(
        asset_id=asset_id,
        generated_at=generated_at,
        timeframe=timeframe,
        action=action,
        confidence=confidence,
        entry_reference=price if directional else None,
        stop_loss_reference=(
            price * (1 - side * stop_distance_pct) if directional else None
        ),
        take_profit_reference=(
            price * (1 + side * target_distance_pct) if directional else None
        ),
        risk_level=RiskLevel.MEDIUM,
        thesis="deterministic test thesis",
        supporting_factors=["factor-one", "factor-two"],
        opposing_factors=["factor-three"],
        invalidation_conditions=["stop breach"],
        data_sources=[
            DataSourceRef(
                name="scripted",
                kind="market_data",
                status="simulated",
                retrieved_at=generated_at,
            )
        ],
        models_used=["fake-quick"],
    )


def default_config(**overrides: Any) -> PaperTradingConfig:
    kwargs: dict[str, Any] = {
        "environment": "test",
        "enabled": True,
        "account_id": "paper-test",
        "initial_capital": 10_000.0,
        "sizing": SizingPolicy(mode="fixed_notional", value=1_000.0),
        "risk": PaperRiskLimits(),
        "schedules": [ScheduleEntry(asset_id="XAUUSD", timeframe="1h")],
    }
    kwargs.update(overrides)
    return PaperTradingConfig(**kwargs)


def make_engine(
    *,
    store_root: Path,
    bars: list[Bar],
    clock: Clock,
    config: PaperTradingConfig | None = None,
    runner: FakeRunner | None = None,
) -> tuple[PaperTradingEngine, FakeRunner]:
    cfg = config or default_config()
    store = JsonPaperStateStore(
        store_root, environment=cfg.environment, account_id=cfg.account_id
    )
    fake_runner = runner or FakeRunner()
    engine = PaperTradingEngine(
        config=cfg,
        store=store,
        provider=ScriptedProvider(bars),
        runner=fake_runner,
        now_fn=clock,
    )
    return engine, fake_runner


__all__ = [
    "Clock",
    "FakeRunner",
    "ScriptedProvider",
    "T0",
    "default_config",
    "make_bar",
    "make_engine",
    "make_signal",
    "scenario_bars",
]
