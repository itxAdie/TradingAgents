"""Phase 2 — research-signal cache and AIResearchStrategy wiring (offline).

The AI path is the Phase 1 ``ResearchEngine`` behind an injectable clock.
Here the engine itself is stubbed; what's under test is the *wiring*:
component disabling, usage counting, cache key identity, and hit/miss
accounting — all without any LLM or network access.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests._research_factories import make_bars
from tradingagents.backtest.clock import SimulationClock
from tradingagents.backtest.engine import AIResearchStrategy, slice_upto
from tradingagents.backtest.historical.replay_provider import ReplayMarketDataProvider
from tradingagents.backtest.research_cache import ResearchCache
from tradingagents.marketdata.models import DataStatus, OhlcvSeries
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.research.schemas import (
    DataSourceRef,
    ResearchSignal,
    RiskLevel,
    SignalAction,
)

UTC = timezone.utc


def _dataset(count: int = 40) -> OhlcvSeries:
    return OhlcvSeries(
        asset_id="XAUUSD", timeframe=Timeframe.H1,
        source="yahoo", status=DataStatus.HISTORICAL,
        bars=make_bars(count, end=datetime(2025, 7, 1, tzinfo=UTC),
                       hours_step=1, zigzag_pct_per_bar=0.3),
    )


def _signal(action: SignalAction = SignalAction.BUY) -> ResearchSignal:
    return ResearchSignal(
        asset_id="XAUUSD", generated_at=datetime(2025, 7, 1, tzinfo=UTC),
        timeframe="1h", action=action, confidence=0.8, entry_reference=100.0,
        stop_loss_reference=95.0, take_profit_reference=110.0,
        risk_level=RiskLevel.MEDIUM, thesis="cached thesis",
        supporting_factors=["f"], models_used=["stub-model"],
        data_sources=[DataSourceRef(name="replay", kind="market_data", status="historical")],
    )


class _StubEngine:
    """Stands in for ResearchEngine; returns queued signals, counts runs."""

    def __init__(self, signals: list[ResearchSignal | None]):
        self.queue = list(signals)
        self.runs = 0
        self.seen_now_fn = None
        self.seen_disabled = None

    def run(self, asset_id: str, timeframe: str):
        self.runs += 1
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item

        class _R:
            signal = item

        return _R()


@pytest.fixture()
def stubbed_engine(monkeypatch):
    holder: dict[str, _StubEngine] = {}

    def _factory(signals):
        stub = _StubEngine(signals)

        class _RealEngine:
            def __init__(self, *, config, provider, llm, now_fn, disabled_components):
                stub.seen_now_fn = now_fn
                stub.seen_disabled = disabled_components

            def run(self, asset_id, timeframe):
                return stub.run(asset_id, timeframe)

        monkeypatch.setattr(
            "tradingagents.research.engine.ResearchEngine", _RealEngine,
        )
        holder["stub"] = stub
        return stub

    return _factory


class _DummyLLM:
    """Inert stand-in so attach() wraps this instead of building a client."""

    def invoke(self, prompt):  # pragma: no cover - never called here
        raise AssertionError("stub engine must not see raw LLM calls")


def _strategy(tmp_path: Path, stub_factory, signals) -> tuple[AIResearchStrategy, object]:
    strat = AIResearchStrategy(
        asset_id="XAUUSD",
        timeframe=Timeframe.H1,
        research_config={
            "llm_provider": "openai", "quick_think_llm": "stub",
            "deep_think_llm": "stub", "backend_url": None,
        },
        llm=_DummyLLM(),  # counted wrapper attaches around this; no client built
        enable_macro=False,
        research_cache=ResearchCache(tmp_path / "cache"),
    )
    ds = _dataset()
    clock = SimulationClock(ds.bars[-1].timestamp)
    provider = ReplayMarketDataProvider(ds, clock)
    stub = stub_factory(signals)
    strat.attach(provider)
    return strat, stub


def test_attach_binds_simulation_clock_and_disables_components(stubbed_engine, tmp_path) -> None:
    ds = _dataset(6)
    clock = SimulationClock(ds.bars[3].timestamp)
    provider = ReplayMarketDataProvider(ds, clock)

    strat = AIResearchStrategy(
        asset_id="XAUUSD", timeframe=Timeframe.H1,
        research_config={"llm_provider": "openai", "quick_think_llm": "x",
                         "deep_think_llm": "x", "backend_url": None},
    )
    stub = stubbed_engine([_signal()])
    strat.attach(provider)

    # Live-only components are always off; macro off unless enabled.
    assert {"news", "sentiment", "macro"} <= set(stub.seen_disabled)
    # The research engine's "now" is the simulation clock, not wall time.
    assert stub.seen_now_fn() == ds.bars[3].timestamp
    clock.set(ds.bars[4].timestamp)
    assert stub.seen_now_fn() == ds.bars[4].timestamp


def test_attach_enables_macro_when_explicitly_allowed(stubbed_engine, tmp_path) -> None:
    ds = _dataset(5)
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[-1].timestamp))
    strat = AIResearchStrategy(
        asset_id="XAUUSD", timeframe=Timeframe.H1,
        research_config={"llm_provider": "openai", "quick_think_llm": "x",
                         "deep_think_llm": "x", "backend_url": None},
        enable_macro=True,
    )
    stub = stubbed_engine([_signal()])
    strat.attach(provider)
    assert set(stub.seen_disabled) == {"news", "sentiment"}


def test_attach_builds_default_client_when_llm_is_none(
    stubbed_engine, tmp_path, monkeypatch,
) -> None:
    """No injected LLM => the same default client the live engine builds,
    wrapped in the usage counter (cost tracking never silently skipped)."""
    built: dict[str, object] = {}

    class _FakeClient:
        def __init__(self):
            self.llm = _DummyLLM()

        def get_llm(self):
            return self.llm

    def _fake_create_llm_client(*, provider, model, base_url=None):
        built["provider"] = provider
        built["model"] = model
        return _FakeClient()

    monkeypatch.setattr(
        "tradingagents.llm_clients.create_llm_client", _fake_create_llm_client,
    )
    ds = _dataset(5)
    provider = ReplayMarketDataProvider(ds, SimulationClock(ds.bars[-1].timestamp))
    strat = AIResearchStrategy(
        asset_id="XAUUSD", timeframe=Timeframe.H1,
        research_config={"llm_provider": "openai", "quick_think_llm": "gpt-test",
                         "deep_think_llm": "gpt-test", "backend_url": None},
    )
    stubbed_engine([_signal()])
    strat.attach(provider)
    assert built == {"provider": "openai", "model": "gpt-test"}
    # The counting wrapper wraps the client's LLM, and the engine gets it.
    assert strat.usage_llm is not None
    assert strat.usage_llm._inner.__class__ is _DummyLLM


def test_generate_runs_engine_and_caches_signal(stubbed_engine, tmp_path) -> None:
    strat, stub = _strategy(tmp_path, stubbed_engine, [_signal()])
    ds = _dataset()
    visible = slice_upto(ds, ds.bars[20].timestamp)

    first = strat.generate(asset_id="XAUUSD", timeframe="1h", visible=visible,
                           now=ds.bars[20].timestamp)
    assert first is not None and first.action is SignalAction.BUY
    assert stub.runs == 1
    assert strat.cache.misses == 1 and strat.cache.hits == 0

    # Identical inputs -> cache hit, engine NOT re-run.
    second = strat.generate(asset_id="XAUUSD", timeframe="1h", visible=visible,
                            now=ds.bars[20].timestamp)
    assert second is not None and second.thesis == "cached thesis"
    assert stub.runs == 1  # engine untouched
    assert strat.cache.hits == 1


def test_cache_misses_when_visible_history_grows(stubbed_engine, tmp_path) -> None:
    strat, stub = _strategy(
        tmp_path, stubbed_engine, [_signal(SignalAction.BUY), _signal(SignalAction.SELL)],
    )
    ds = _dataset()
    v20 = slice_upto(ds, ds.bars[20].timestamp)
    v21 = slice_upto(ds, ds.bars[21].timestamp)
    s1 = strat.generate(asset_id="XAUUSD", timeframe="1h", visible=v20,
                        now=ds.bars[20].timestamp)
    s2 = strat.generate(asset_id="XAUUSD", timeframe="1h", visible=v21,
                        now=ds.bars[21].timestamp)
    assert s1.action is SignalAction.BUY and s2.action is SignalAction.SELL
    assert stub.runs == 2
    assert strat.cache.misses == 2 and strat.cache.hits == 0


def test_cache_persists_across_instances(tmp_path, stubbed_engine) -> None:
    strat, stub = _strategy(tmp_path, stubbed_engine, [_signal()])
    ds = _dataset()
    visible = slice_upto(ds, ds.bars[15].timestamp)
    strat.generate(asset_id="XAUUSD", timeframe="1h", visible=visible,
                   now=ds.bars[15].timestamp)

    fresh_cache = ResearchCache(tmp_path / "cache")
    key_hit = fresh_cache.get(
        asset_id="XAUUSD", timeframe="1h",
        decision_at=ds.bars[15].timestamp.isoformat(),
        visible=visible, model_ids=strat.model_ids,
        config_hash=strat.config_hash(),
    )
    assert key_hit is not None and key_hit.thesis == "cached thesis"
    assert fresh_cache.hits == 1


def test_corrupt_cache_entry_behaves_as_miss(tmp_path, stubbed_engine) -> None:
    strat, stub = _strategy(tmp_path, stubbed_engine, [_signal()])
    ds = _dataset()
    visible = slice_upto(ds, ds.bars[10].timestamp)
    strat.generate(asset_id="XAUUSD", timeframe="1h", visible=visible,
                   now=ds.bars[10].timestamp)

    # Corrupt every stored entry.
    for f in (tmp_path / "cache").glob("*.json"):
        f.write_text("{not json", encoding="utf-8")

    fresh = ResearchCache(tmp_path / "cache")
    got = fresh.get(
        asset_id="XAUUSD", timeframe="1h",
        decision_at=ds.bars[10].timestamp.isoformat(),
        visible=visible, model_ids=strat.model_ids,
        config_hash=strat.config_hash(),
    )
    assert got is None
    assert fresh.misses == 1 and fresh.hits == 0


def test_prompt_version_changes_cache_key(tmp_path, stubbed_engine) -> None:
    strat, _ = _strategy(tmp_path, stubbed_engine, [_signal()])
    ds = _dataset()
    visible = slice_upto(ds, ds.bars[5].timestamp)
    strat.generate(asset_id="XAUUSD", timeframe="1h", visible=visible,
                   now=ds.bars[5].timestamp)
    # A different prompt version would produce a different key string.
    k1 = ResearchCache._key(
        asset_id="XAUUSD", timeframe="1h",
        decision_at=ds.bars[5].timestamp.isoformat(),
        visible_bars_hash="abc", model_ids=["m"], config_hash="c",
    )
    assert len(k1) == 64
