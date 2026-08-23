"""Offline integration and failure-path tests for the research engine.

All external seams are faked: the market-data provider is a stub, LLM calls
run through FakeStructuredLLM, and news/sentiment/macro fetchers are
monkeypatched. No network access happens in this module.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import tradingagents.research.engine as engine_mod
from tests._research_factories import (
    FakeProvider,
    FakeStructuredLLM,
    make_series,
    utc_now,
)
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.research.engine import ResearchEngine
from tradingagents.research.schemas import (
    BearCase,
    BullCase,
    MacroAnalysis,
    NewsAnalysis,
    NewsItem,
    ResearchManagerVerdict,
    SentimentAnalysis,
    TechnicalAnalysis,
)

FRESH_END = utc_now() - timedelta(minutes=30)


def _technical():
    return TechnicalAnalysis(
        summary="Price above rising averages per snapshot.",
        trend="up", momentum="bullish", volatility="medium",
        key_levels=["SMA50 at 2390"],
    )


def _macro():
    return MacroAnalysis(
        available=True, direction="supportive_bullish",
        summary="Fetched series show supportive conditions.",
        factors_considered=["fed_funds_rate"], data_notes="3 FRED series used",
    )


def _news():
    return NewsAnalysis(
        available=True, tone="neutral", summary="Quiet tape.",
        items=[NewsItem(headline="Fed holds rates", source="Reuters",
                        published_at=None, relevance="high", sentiment="neutral")],
    )


def _sentiment():
    return SentimentAnalysis(available=True, sources=["stocktwits"], band="Bullish", score=6.5)


def _bull():
    return BullCase(thesis="Trend intact.", key_points=["up regime", "supportive macro"])


def _bear():
    return BearCase(thesis="Stretched short term.", key_points=["rsi elevated", "news quiet"])


def _verdict(direction="BUY"):
    return ResearchManagerVerdict(
        direction=direction, consensus="Technical case carried.",
        self_reported_confidence=0.8,
        invalidation_conditions=[], risks=["headline risk"],
    )


def _full_queue(**overrides):
    queue = [_technical(), _macro(), _news(), _sentiment(), _bull(), _bear(), _verdict()]
    for i, value in overrides.items():  # e.g. technical=Exception(...)
        index = {
            "technical": 0, "macro": 1, "news": 2, "sentiment": 3,
            "bull": 4, "bear": 5, "manager": 6,
        }[i]
        queue[index] = value
    return queue


def _engine(queue, provider=None):
    return ResearchEngine(
        config={"quick_think_llm": "q-model", "deep_think_llm": "d-model"},
        # Mild uptrend so the *deterministic* fallback path (used when LLM
        # agents fail) classifies trend/momentum as up/bullish.
        provider=provider
        or FakeProvider(make_series(300, end=FRESH_END, drift_pct_per_bar=0.05)),
        llm=FakeStructuredLLM(queue),
    )


@pytest.fixture(autouse=True)
def _patch_external_seams(monkeypatch):
    monkeypatch.setattr(
        engine_mod, "get_news_yfinance",
        lambda ticker, start, end: "### Real headline (source: Reuters)\nbody",
    )
    monkeypatch.setattr(
        engine_mod, "get_global_news_yfinance",
        lambda curr_date: "### Global macro headline (source: Bloomberg)\nbody",
    )
    monkeypatch.setattr(
        engine_mod, "fetch_stocktwits_messages",
        lambda ticker: "user1: gold to the moon LPS",
    )
    monkeypatch.setattr(
        engine_mod, "fetch_reddit_posts", lambda ticker: "post about gold ETFs"
    )
    monkeypatch.setattr(
        engine_mod, "route_to_vendor",
        lambda method, indicator, date, lookback: "## FRED fed_funds_rate\n5.33%",
    )


@pytest.mark.integration
class TestHappyPath:
    def test_full_run_produces_report_and_buy_signal(self):
        result = _engine(_full_queue()).run("XAUUSD", "1h")
        r = result.report
        assert r.asset_id == "XAUUSD"
        assert r.technical_analysis.trend == "up"
        assert r.macro_analysis.available
        assert r.news_analysis.items[0].source == "Reuters"
        assert r.sentiment_analysis.band == "Bullish"
        assert r.bull_case.key_points and r.bear_case.key_points
        assert r.manager_verdict.direction == "BUY"
        assert r.agent_failures == []
        assert r.disclaimer == "RESEARCH SIGNAL — NOT EXECUTED"

        s = result.signal
        assert s is not None and s.action.value == "BUY"
        assert s.stop_loss_reference < s.entry_reference < s.take_profit_reference
        assert s.disclaimer == "RESEARCH SIGNAL — NOT EXECUTED"
        assert "q-model" in s.models_used

    def test_crypto_asset_supported(self):
        result = _engine(
            _full_queue(),
            provider=FakeProvider(
                make_series(300, asset_id="BTCUSD", end=FRESH_END)
            ),
        ).run("BTCUSD", "4h")
        assert result.signal is not None
        assert result.report.asset_id == "BTCUSD"

    def test_all_timeframes_accepted(self):
        for tf in ("15m", "1h", "4h", "1d"):
            result = _engine(_full_queue()).run("XAUUSD", tf)
            assert result.signal is not None, tf


@pytest.mark.integration
class TestFailurePaths:
    def test_agent_failure_is_isolated_and_recorded(self):
        # Technical analyst blows up; every other stage still runs.
        result = _engine(_full_queue(technical=RuntimeError("llm down"))).run("XAUUSD", "1h")
        r = result.report
        assert r.technical_analysis is None
        assert [f.agent for f in r.agent_failures] == ["technical_analyst"]
        # Missing analysis must be visible, not fabricated.
        assert any("technical_analyst" in f for f in result.signal.opposing_factors)
        assert result.signal.action.value == "BUY"  # verdict survived

    def test_malformed_structured_output_degrades(self):
        # Model answers with prose instead of the schema -> section unavailable.
        result = _engine(_full_queue(news="I cannot comply")).run("XAUUSD", "1h")
        assert result.report.news_analysis is None
        assert any(f.agent == "news_analyst" for f in result.report.agent_failures)

    def test_provider_error_means_no_signal(self):
        engine = _engine(
            _full_queue(),
            provider=FakeProvider(error=NoMarketDataError("XAUUSD")),
        )
        result = engine.run("XAUUSD", "1h")
        assert result.signal is None
        assert "market data" in result.no_signal_reason.lower()
        assert any(f.agent == "market_data" for f in result.report.agent_failures)

    def test_stale_market_data_blocks_signal(self):
        old_end = utc_now() - timedelta(days=30)
        stale = make_series(300, end=old_end)
        result = _engine(_full_queue(), provider=FakeProvider(stale)).run("XAUUSD", "1h")
        assert result.signal is None
        assert "stale" in result.no_signal_reason.lower()
        assert any("freshness" in risk.lower() for risk in result.report.risks)

    def test_every_agent_failing_still_yields_deterministic_signal(self):
        boom = RuntimeError("no llm")
        result = _engine([boom] * 7).run("XAUUSD", "1h")
        assert len(result.report.agent_failures) == 7
        # Deterministic fallback: up trend + bullish momentum -> BUY.
        assert result.signal is not None
        assert result.signal.action.value == "BUY"
        assert result.report.manager_verdict is None
