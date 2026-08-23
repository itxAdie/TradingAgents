"""Research engine — Phase 1 research-only pipeline.

Orchestrates: market data via a :class:`MarketDataProvider` → deterministic
indicators → per-section LLM agents (structured output only) → deterministic
signal assembly. Design notes:

- Orchestration is plain sequential Python rather than a LangGraph runtime:
  the flow is linear, every stage needs an isolated try/except failure
  boundary (graceful degradation is a hard requirement), and direct calls are
  far easier to test offline. Stage callables are node-shaped, so migrating
  onto LangGraph later is mechanical if conditional routing ever appears.
- LLM sections use structured output ONLY. A failed structured call marks the
  section unavailable instead of falling back to prose, because downstream
  consumers require schemas and fabricated structure is worse than none.
- The engine never executes anything; the only artifact is a labelled
  :class:`ResearchSignal`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel

from tradingagents.agents.utils.structured import bind_structured
from tradingagents.analysis.indicators import TechnicalSnapshot, compute_indicators, render_snapshot
from tradingagents.assets.registry import AssetSpec, get_asset
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.reddit import fetch_reddit_posts
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages
from tradingagents.dataflows.yfinance_news import (
    get_global_news_yfinance,
    get_news_yfinance,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.marketdata.models import DataFreshness, OhlcvSeries
from tradingagents.marketdata.provider import MarketDataProvider
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.marketdata.yahoo_provider import YahooMarketDataProvider
from tradingagents.research.assembly import AssembledResult, assemble
from tradingagents.research.logging import log_event
from tradingagents.research.schemas import (
    AgentFailure,
    BearCase,
    BullCase,
    DataSourceRef,
    MacroAnalysis,
    NewsAnalysis,
    ResearchManagerVerdict,
    ResearchReport,
    SentimentAnalysis,
    TechnicalAnalysis,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Bars requested from the provider: enough for SMA200 on any timeframe.
DEFAULT_BAR_LIMIT = 300
NEWS_LOOKBACK_DAYS = 3
# FRED series pulled for gold macro context (alias names understood by fred.py).
_GOLD_MACRO_SERIES = ("fed_funds_rate", "cpi", "10y_treasury")
_MACRO_LOOKBACK_DAYS = 90


def _invoke_structured(llm: Any, schema: type[T], prompt: str) -> T | None:
    """One structured attempt; ``None`` on any failure (caller records it).

    Binding per call keeps this trivially correct (no shared mutable cache);
    ``with_structured_output`` is a cheap client-side wrap.
    """
    structured = bind_structured(llm, schema, schema.__name__)
    if structured is None:
        return None
    try:
        result = structured.invoke(prompt)
    except Exception as exc:  # noqa: BLE001 — section degrades, never crashes
        logger.warning("structured invocation failed for %s: %s", schema.__name__, exc)
        return None
    return result if isinstance(result, schema) else None


# ---------------------------------------------------------------------------
# Agent node factories (node-shaped callables over a mutable state dict)
# ---------------------------------------------------------------------------


def create_technical_analyst_node(llm: Any):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        snapshot: TechnicalSnapshot | None = state.get("snapshot")
        if snapshot is None:
            raise RuntimeError("technical analyst requires a verified snapshot")
        prompt = (
            "You are a technical analyst for a RESEARCH-ONLY system.\n\n"
            f"{render_snapshot(snapshot)}\n\n"
            "Produce the structured analysis. Cite only numbers present above."
        )
        result = _invoke_structured(llm, TechnicalAnalysis, prompt)
        if result is None:
            raise RuntimeError(
                "structured output unavailable/failed for TechnicalAnalysis"
            )
        return {"technical_analysis": result}

    return node


def create_macro_analyst_node(llm: Any):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        asset: AssetSpec = state["asset"]
        macro_text: str = state.get("macro_text") or ""
        news_text: str = state.get("news_text") or ""
        has_data = bool(macro_text.strip())
        factors = ", ".join(asset.macro_context) or "general macro conditions"
        prompt = (
            "You are a market/macro analyst for a RESEARCH-ONLY system.\n"
            f"Asset: {asset.display_name} ({asset.asset_class.value}).\n"
            f"Factors typically relevant: {factors}.\n\n"
            + (f"MACRO DATA (real, fetched):\n{macro_text}\n\n" if has_data else
               "NO macro series data was available. You must set available=false "
               "and must not invent rates or events.\n\n")
            + (f"RECENT HEADLINES (real, fetched):\n{news_text[:4000]}\n\n"
               if news_text.strip() else "")
            + _invoke_note()
        )
        result = _invoke_structured(llm, MacroAnalysis, prompt)
        if result is None:
            raise RuntimeError("structured output unavailable/failed for MacroAnalysis")
        return {"macro_analysis": result}

    return node


def create_news_analyst_node(llm: Any):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        news_text: str = state.get("news_text") or ""
        if not news_text.strip():
            raise RuntimeError("no news text was gathered")
        prompt = (
            "You are a news analyst for a RESEARCH-ONLY system.\n"
            "Below is REAL fetched news text. Structure it: extract items "
            "(headline/source/published time only when literally present), rate "
            "relevance to the asset under analysis, and give each item a "
            "sentiment. Never invent items.\n\n"
            f"{news_text[:8000]}\n\n{_invoke_note()}"
        )
        result = _invoke_structured(llm, NewsAnalysis, prompt)
        if result is None:
            raise RuntimeError("structured output unavailable/failed for NewsAnalysis")
        return {"news_analysis": result}

    return node


def create_sentiment_node(llm: Any):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        sentiment_text: str = state.get("sentiment_text") or ""
        sources: list[str] = state.get("sentiment_sources") or []
        if not sentiment_text.strip():
            raise RuntimeError("no social-sentiment text was gathered")
        bands = "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish"
        prompt = (
            "You are a sentiment analyst for a RESEARCH-ONLY system.\n"
            "Below are REAL fetched social posts/messages. Judge overall retail "
            f"sentiment as one band of: {bands}, plus a 0-10 score "
            "(0=max bearish, 5=neutral, 10=max bullish). Ground everything in "
            "the provided posts only.\n\n"
            f"{sentiment_text[:6000]}\n\n{_invoke_note()}"
        )
        result = _invoke_structured(llm, SentimentAnalysis, prompt)
        if result is None:
            raise RuntimeError("structured output unavailable/failed for SentimentAnalysis")
        if not result.sources:
            result = result.model_copy(update={"sources": list(sources)})
        return {"sentiment_analysis": result}

    return node


def create_bull_researcher_node(llm: Any):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        prompt = _debate_prompt(state, side="bullish")
        result = _invoke_structured(llm, BullCase, prompt)
        if result is None:
            raise RuntimeError("structured output unavailable/failed for BullCase")
        return {"bull_case": result}

    return node


def create_bear_researcher_node(llm: Any):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        prompt = _debate_prompt(state, side="bearish")
        result = _invoke_structured(llm, BearCase, prompt)
        if result is None:
            raise RuntimeError("structured output unavailable/failed for BearCase")
        return {"bear_case": result}

    return node


def create_research_manager_node(llm: Any):
    def node(state: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "You are the Research Manager for a RESEARCH-ONLY system.\n"
            "Weigh the bull and bear cases plus all available sections and "
            "decide ONE direction: BUY, SELL, or HOLD. Reserve HOLD for "
            "genuinely balanced evidence. Report your calibrated confidence in "
            "[0,1] and observable invalidation conditions checkable against "
            "the provided data.\n\n"
            f"{_render_sections_for_debate(state)}\n{_invoke_note()}"
        )
        result = _invoke_structured(llm, ResearchManagerVerdict, prompt)
        if result is None:
            raise RuntimeError(
                "structured output unavailable/failed for ResearchManagerVerdict"
            )
        return {"manager_verdict": result}

    return node


def _invoke_note() -> str:
    return (
        "Respond through the structured schema only. Use exclusively the "
        "evidence above; mark anything missing explicitly."
    )


def _render_sections_for_debate(state: dict[str, Any]) -> str:
    parts = [f"ASSET: {state['asset'].display_name} @ {state['timeframe'].value}"]
    snap: TechnicalSnapshot | None = state.get("snapshot")
    if snap is not None:
        parts.append(render_snapshot(snap))
    for key, title in (
        ("technical_analysis", "TECHNICAL ANALYSIS"),
        ("macro_analysis", "MACRO ANALYSIS"),
        ("news_analysis", "NEWS ANALYSIS"),
        ("sentiment_analysis", "SENTIMENT ANALYSIS"),
    ):
        value = state.get(key)
        if value is not None:
            parts.append(f"{title}:\n{value.model_dump_json(exclude_none=True)}")
        else:
            parts.append(f"{title}: UNAVAILABLE (do not assume its content)")
    return "\n\n".join(parts)


def _debate_prompt(state: dict[str, Any], side: str) -> str:
    return (
        f"You are the {side} researcher for a RESEARCH-ONLY system. Build the "
        f"strongest honest {side} case strictly from the material below; if "
        "material is marked UNAVAILABLE you may not lean on it.\n\n"
        f"{_render_sections_for_debate(state)}\n{_invoke_note()}"
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ResearchEngine:
    """Research-only entry point: ``(report, signal|None)`` per run."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        provider: MarketDataProvider | None = None,
        llm: Any = None,
    ):
        self.config = config or DEFAULT_CONFIG.copy()
        self.provider = provider or YahooMarketDataProvider()
        self.llm = llm  # None -> created lazily in run() so tests can inject
        self.nodes = {
            "technical_analyst": create_technical_analyst_node,
            "macro_analyst": create_macro_analyst_node,
            "news_analyst": create_news_analyst_node,
            "sentiment_analyst": create_sentiment_node,
            "bull_researcher": create_bull_researcher_node,
            "bear_researcher": create_bear_researcher_node,
            "research_manager": create_research_manager_node,
        }

    # -- public API ---------------------------------------------------------

    def run(self, asset_id: str, timeframe: Timeframe | str) -> AssembledResult:
        started = datetime.now(timezone.utc)
        tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe(str(timeframe).lower())
        asset = get_asset(asset_id)
        failures: list[AgentFailure] = []
        data_sources: list[DataSourceRef] = []

        log_event(
            "research_started", asset_id=asset.asset_id, timeframe=tf.value,
            provider=self.provider.name,
        )

        state: dict[str, Any] = {
            "asset": asset,
            "timeframe": tf,
            "failures": failures,
            "data_sources": data_sources,
        }

        # 1. Market data + indicators (mandatory for a signal).
        freshness = DataFreshness.STALE
        series = self._gather_market_data(asset, tf, state, failures, data_sources)
        if series is not None:
            freshness = self._assess_freshness(series)
        # 2..4 optional enrichment — each degrades independently.
        self._gather_news(asset, state)
        self._gather_sentiment(asset, state)
        self._gather_macro(asset, state)

        # 5. LLM agents in order.
        llm = self.llm if self.llm is not None else self._create_llm()
        for name in (
            "technical_analyst",
            "macro_analyst",
            "news_analyst",
            "sentiment_analyst",
            "bull_researcher",
            "bear_researcher",
            "research_manager",
        ):
            self._execute_agent(name, state, llm)

        generated_at = datetime.now(timezone.utc)
        models_used = [
            str(self.config.get("quick_think_llm")),
            str(self.config.get("deep_think_llm")),
        ]

        verdict = state.get("manager_verdict")
        report = self._build_report(
            asset=asset, tf=tf, generated_at=generated_at, started=started,
            state=state, freshness=freshness, failures=failures,
            data_sources=data_sources, models_used=models_used,
        )

        if series is None:
            reason = "market data unavailable; no signal emitted"
        elif freshness is DataFreshness.STALE:
            reason = (
                f"latest bar older than the {tf.value} staleness window; "
                "refusing to emit a signal from stale data"
            )
        else:
            signal, reason = assemble(
                asset=asset, timeframe=tf, generated_at=generated_at,
                snapshot=state.get("snapshot"),
                technical=state.get("technical_analysis"),
                macro=state.get("macro_analysis"),
                news=state.get("news_analysis"),
                sentiment=state.get("sentiment_analysis"),
                bull=state.get("bull_case"),
                bear=state.get("bear_case"),
                verdict=verdict,
                failures=failures,
                data_sources=data_sources,
                models_used=models_used,
            )
            if signal is not None:
                log_event(
                    "signal_generated", logger=logger, asset_id=asset.asset_id,
                    timeframe=tf.value, action=signal.action.value,
                    confidence=signal.confidence,
                )
                log_event(
                    "research_completed", asset_id=asset.asset_id,
                    duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                    outcome="signal",
                )
                return AssembledResult(report, signal)

        log_event(
            "research_completed", asset_id=asset.asset_id, outcome="no_signal",
            reason=reason, duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        )
        return AssembledResult(report, None, reason)

    # -- gathering helpers ---------------------------------------------------

    def _gather_market_data(
        self,
        asset: AssetSpec,
        tf: Timeframe,
        state: dict[str, Any],
        failures: list[AgentFailure],
        data_sources: list[DataSourceRef],
    ) -> OhlcvSeries | None:
        try:
            series = self.provider.get_ohlcv(asset, tf, limit=DEFAULT_BAR_LIMIT)
        except Exception as exc:  # noqa: BLE001 — mandatory-but-degrading
            logger.error("market data failed for %s: %s", asset.asset_id, exc)
            failures.append(AgentFailure(agent="market_data", error_type=type(exc).__name__, message=str(exc)))
            data_sources.append(DataSourceRef(name=self.provider.name, kind="market_data", status="unavailable", detail=str(exc)))
            log_event("agent_failed", agent="market_data", error_type=type(exc).__name__)
            return None
        data_sources.append(DataSourceRef(
            name=self.provider.name, kind="market_data", status=series.status.value,
            retrieved_at=datetime.now(timezone.utc),
            detail=f"{len(series.bars)} bars, latest {series.latest_timestamp}",
        ))
        snapshot = compute_indicators(series)
        state["series"] = series
        state["snapshot"] = snapshot
        log_event(
            "data_fetched", asset_id=asset.asset_id, source=self.provider.name,
            status=series.status.value, bars=len(series.bars),
            latest_bar=series.latest_timestamp,
        )
        return series

    def _assess_freshness(self, series: OhlcvSeries) -> DataFreshness:
        tf = series.timeframe
        latest = series.latest_timestamp
        if latest is None:
            return DataFreshness.STALE
        age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
        # Timeframe-specific windows live in one place (timeframes.py) and are
        # deliberately generous so weekends/holidays don't false-positive.
        return DataFreshness.FRESH if age_hours <= tf.staleness_hours() else DataFreshness.STALE

    def _gather_news(self, asset: AssetSpec, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        blocks: list[str] = []
        try:
            ticker_news = get_news_yfinance(asset.yahoo_symbol, start, end)
            if not ticker_news.startswith(("Error", "No news")):
                blocks.append(ticker_news)
            global_news = get_global_news_yfinance(end)
            if not global_news.startswith(("Error", "No global news")):
                blocks.append(global_news)
        except Exception as exc:  # noqa: BLE001 — optional enrichment
            logger.warning("news gathering failed: %s", exc)
        text = "\n\n".join(blocks).strip()
        state["news_text"] = text
        state.setdefault("data_sources").append(DataSourceRef(
            name="yahoo_news", kind="news",
            status="ok" if text else "unavailable",
            retrieved_at=now,
        ))

    def _gather_sentiment(self, asset: AssetSpec, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        blocks: list[str] = []
        sources: list[str] = []
        try:
            tw = fetch_stocktwits_messages(asset.asset_id)
            if tw and "unable" not in tw.lower() and "no messages" not in tw.lower():
                blocks.append(f"[stocktwits]\n{tw}")
                sources.append("stocktwits")
        except Exception as exc:  # noqa: BLE001
            logger.warning("stocktwits failed: %s", exc)
        try:
            rd = fetch_reddit_posts(asset.yahoo_symbol)
            if rd and "no posts" not in rd.lower() and "error" not in rd.lower():
                blocks.append(f"[reddit]\n{rd}")
                sources.append("reddit")
        except Exception as exc:  # noqa: BLE001
            logger.warning("reddit failed: %s", exc)
        state["sentiment_text"] = "\n\n".join(blocks).strip()
        state["sentiment_sources"] = sources
        state.setdefault("data_sources").append(DataSourceRef(
            name="+".join(sources) or "social", kind="sentiment",
            status="ok" if sources else "unavailable", retrieved_at=now,
        ))

    def _gather_macro(self, asset: AssetSpec, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        blocks: list[str] = []
        series_names = _GOLD_MACRO_SERIES if asset.asset_class.value != "crypto" else ()
        for indicator in series_names:
            try:
                block = route_to_vendor("get_macro_indicators", indicator, today, _MACRO_LOOKBACK_DAYS)
                if isinstance(block, str) and block and not block.startswith(("NO_DATA", "DATA_UNAVAILABLE")):
                    blocks.append(block)
            except Exception as exc:  # noqa: BLE001 — optional enrichment
                logger.warning("macro series %s failed: %s", indicator, exc)
        text = "\n\n".join(blocks).strip()
        state["macro_text"] = text
        state.setdefault("data_sources").append(DataSourceRef(
            name="fred", kind="macro", status="ok" if text else "unavailable",
            retrieved_at=now,
        ))

    # -- agents ---------------------------------------------------------------

    def _execute_agent(self, name: str, state: dict[str, Any], llm: Any) -> None:
        factory = self.nodes[name]
        log_event("agent_started", agent=name)
        try:
            update = factory(llm)(state)
            state.update(update)
            log_event("agent_completed", agent=name)
        except Exception as exc:  # noqa: BLE001 — isolation is the contract
            failures_list: list[AgentFailure] = state.setdefault("failures", [])
            failures_list.append(AgentFailure(
                agent=name, error_type=type(exc).__name__, message=str(exc),
            ))
            logger.error("agent %s failed: %s", name, exc)
            log_event("agent_failed", agent=name, error_type=type(exc).__name__)

    # -- reporting -------------------------------------------------------------

    def _build_report(
        self,
        *,
        asset: AssetSpec,
        tf: Timeframe,
        generated_at: datetime,
        started: datetime,
        state: dict[str, Any],
        freshness: DataFreshness,
        failures: list[AgentFailure],
        data_sources: list[DataSourceRef],
        models_used: list[str],
    ) -> ResearchReport:
        snapshot: TechnicalSnapshot | None = state.get("snapshot")
        verdict = state.get("manager_verdict")
        confidence = 0.0
        breakdown: dict[str, float] = {}
        if snapshot is not None and verdict is not None:
            from tradingagents.analysis import confidence as conf

            news = state.get("news_analysis")
            sentiment = state.get("sentiment_analysis")
            macro = state.get("macro_analysis")
            b = conf.compute_confidence(
                verdict.direction,
                trend=snapshot.trend if snapshot.trend != "unknown" else None,
                momentum=snapshot.momentum if snapshot.momentum != "unknown" else None,
                macro_direction=macro.direction if macro and macro.available else None,
                news_tone=news.tone if news and news.available else None,
                sentiment_band=str(sentiment.band.value) if sentiment and sentiment.available and sentiment.band else None,
                manager_direction=verdict.direction,
                news_available=bool(news and news.available),
                sentiment_available=bool(sentiment and sentiment.available),
                macro_available=bool(macro and macro.available),
                model_confidences=(
                    [verdict.self_reported_confidence]
                    if verdict.self_reported_confidence is not None else []
                ),
            )
            confidence = b.score
            breakdown = {
                "agent_agreement": b.agent_agreement,
                "data_completeness": b.data_completeness,
                "signal_consistency": b.signal_consistency,
                "model_confidence": b.model_confidence,
            }
        risks = list(verdict.risks) if verdict else []
        if freshness is DataFreshness.STALE:
            risks.append("Market data exceeded the freshness window for this timeframe.")
        return ResearchReport(
            asset_id=asset.asset_id,
            display_name=asset.display_name,
            timeframe=tf.value,
            generated_at=generated_at,
            market_data_timestamp=snapshot.latest_bar_timestamp if snapshot else None,
            market_data_status=(
                state["series"].status.value
                if state.get("series") is not None else "unavailable"
            ) + ("" if freshness is DataFreshness.FRESH else " (stale)"),
            technical_analysis=state.get("technical_analysis"),
            macro_analysis=state.get("macro_analysis"),
            news_analysis=state.get("news_analysis"),
            sentiment_analysis=state.get("sentiment_analysis"),
            bull_case=state.get("bull_case"),
            bear_case=state.get("bear_case"),
            manager_verdict=verdict,
            confidence=confidence,
            confidence_breakdown=breakdown,
            risks=risks,
            agent_failures=list(failures),
            data_sources=list(data_sources),
            models_used=models_used,
        )

    def _create_llm(self) -> Any:
        from tradingagents.llm_clients import create_llm_client

        client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["quick_think_llm"],
            base_url=self.config.get("backend_url"),
        )
        return client.get_llm()


__all__ = ["ResearchEngine", "DEFAULT_BAR_LIMIT"]
