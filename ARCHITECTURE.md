# Architecture

This document describes the **CURRENT** architecture (verified against the
repository at v0.3.1, commit `a33fd4c`) and the **PHASE 1** research-platform
architecture, which is now **IMPLEMENTED** (see §"PHASE 1 — Research Platform
(IMPLEMENTED)"). Phase 1 is additive: every pre-existing module behaves as
before; the new packages (`assets/`, `marketdata/`, `analysis/`, `research/`)
and the `research` CLI subcommand are opt-in. Nothing in Phase 1 executes
trades.

---

## 1. CURRENT — High-Level System Overview

TradingAgents is a multi-agent LLM financial research framework built on
LangGraph. A run takes a **ticker + trade date**, assembles a team of
LLM-powered agents (analysts → researchers → trader → risk team → portfolio
manager), lets them call market-data tools mid-reasoning, and produces a
markdown report tree plus a 5-tier portfolio rating.

```
                        ┌────────────────────────────────────────────┐
                        │              Entry points                  │
                        │  cli/main.py (Typer, interactive)          │
                        │  TradingAgentsGraph.propagate(ticker,date) │
                        └───────────────┬────────────────────────────┘
                                        ▼
        normalize_symbol() → resolve instrument identity (yfinance info, cached)
                                        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                     LangGraph StateGraph(AgentState)                      │
│                                                                           │
│  Analysts (tool-calling loops, sequential):                               │
│    Market → Social/Sentiment → News → Fundamentals   (subset selectable)  │
│         │ each analyst: prompt | llm.bind_tools(...) ⇄ ToolNode           │
│         ▼                                                                 │
│  Bull Researcher ⇄ Bear Researcher  (debate rounds)                       │
│         ▼                                                                 │
│  Research Manager   (structured: ResearchPlan)                            │
│         ▼                                                                 │
│  Trader             (structured: TraderProposal)                          │
│         ▼                                                                 │
│  Aggressive ⇄ Conservative ⇄ Neutral  (risk debate rounds)                │
│         ▼                                                                 │
│  Portfolio Manager  (structured: PortfolioDecision)                       │
│         ▼                                                                 │
│  END → SignalProcessor.parse_rating() → memory log + markdown reports     │
└───────────────────────────────────────────────────────────────────────────┘
```

**Output artifacts per run**

- `~/.tradingagents/logs/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`
- Markdown report tree via `tradingagents/reporting.py` (`1_analysts/`,
  `2_research/`, `3_trading/`, `4_risk/`, `5_portfolio/`, `complete_report.md`)
- Append-only decision log `~/.tradingagents/memory/trading_memory.md`
  (past decisions + LLM reflections are re-injected into future runs)

---

## 2. CURRENT — Project Structure

```
tradingagents/
├── agents/
│   ├── analysts/            # market, news, sentiment(social), fundamentals factories
│   ├── managers/            # research_manager.py, portfolio_manager.py
│   ├── researchers/         # bull_researcher.py, bear_researcher.py
│   ├── risk_mgmt/           # aggressive / conservative / neutral debators
│   ├── trader/trader.py
│   ├── schemas.py           # Pydantic structured-output models + render helpers
│   └── utils/
│       ├── agent_states.py  # AgentState (MessagesState TypedDict), debate states
│       ├── agent_utils.py   # tool re-exports, instrument identity/context helpers
│       ├── core_stock_tools.py         # @tool get_stock_data
│       ├── technical_indicators_tools.py # @tool get_indicators
│       ├── fundamental_data_tools.py     # @tool get_fundamentals/balance/cashflow/income
│       ├── news_data_tools.py            # @tool get_news/get_global_news/get_insider_transactions
│       ├── macro_data_tools.py           # @tool get_macro_indicators (FRED)
│       ├── prediction_markets_tools.py   # @tool get_prediction_markets (Polymarket)
│       ├── market_data_validation_tools.py # @tool get_verified_market_snapshot
│       ├── structured.py    # bind_structured / invoke_structured_or_freetext pattern
│       ├── rating.py        # deterministic 5-tier rating extraction
│       └── memory.py        # TradingMemoryLog (decision log + reflections)
├── dataflows/
│   ├── interface.py         # vendor router: route_to_vendor(), VENDOR_METHODS registry
│   ├── config.py            # global get_config()/set_config()
│   ├── errors.py            # VendorError taxonomy
│   ├── symbol_utils.py      # normalize_symbol(): XAUUSD→GC=F, BTCUSD→BTC-USD, …
│   ├── y_finance.py         # OHLCV download + caching; stockstats indicators window
│   ├── stockstats_utils.py  # load_ohlcv cache (TTL), staleness guard (10d), indicators
│   ├── yfinance_news.py     # ticker news + global news (UTC end-exclusive window)
│   ├── alpha_vantage*.py    # AV fundamentals/news/indicator/stock (+rate-limit handling)
│   ├── fred.py              # FRED macro series
│   ├── polymarket.py        # prediction-market probabilities (keyless)
│   ├── stocktwits.py        # StockTwits messages (crypto-mapped symbols)
│   ├── reddit.py            # Reddit posts fallback
│   └── market_data_validator.py # deterministic verified snapshot (OHLCV+indicators)
├── graph/
│   ├── trading_graph.py     # TradingAgentsGraph: init, propagate(), save_reports()
│   ├── setup.py             # GraphSetup.build graph wiring + shared path maps
│   ├── analyst_execution.py # build_analyst_execution_plan(selected_analysts)
│   ├── conditional_logic.py # tool-loop and debate/risk routers
│   ├── propagation.py       # initial state + graph args
│   ├── signal_processing.py # parse PM rating (no extra LLM call)
│   ├── checkpointer.py      # SQLite resume, shape-keyed thread IDs
│   └── reflection.py        # Reflector: outcome reflections for memory log
├── llm_clients/
│   ├── factory.py           # create_llm_client(provider, model, base_url)
│   ├── openai_client.py     # OpenAI + all OpenAI-compatible providers (registry)
│   ├── anthropic/google/azure/bedrock_client.py
│   ├── model_catalog.py     # curated model list per provider
│   ├── capabilities.py      # provider capability flags (structured output etc.)
│   └── api_key_env.py       # API-key env detection/auto-selection
├── reporting.py             # write_report_tree(): markdown tree writer
└── default_config.py        # DEFAULT_CONFIG + TRADINGAGENTS_* env overrides
cli/                         # Typer app: interactive analyze command
tests/                       # 60 offline test modules (pytest, mocked vendors)
main.py                      # minimal programmatic example
test.py                      # ad-hoc indicator timing script (not a test suite)
```

---

## 3. CURRENT — Agent Architecture

| Role | Factory | LLM tier | Structured output | State fields written |
|---|---|---|---|---|
| Market Analyst | `create_market_analyst` | quick | no (prose + table) | `market_report` |
| Sentiment Analyst | `create_sentiment_analyst` | quick | yes (`SentimentReport`) | `sentiment_report` |
| News Analyst | `create_news_analyst` | quick | no | `news_report` |
| Fundamentals Analyst | `create_fundamentals_analyst` | quick | no | `fundamentals_report` |
| Bull/Bear Researcher | `create_bull_researcher` / `bear` | quick | no (debate prose) | `investment_debate_state` |
| Research Manager | `create_research_manager` | deep | yes (`ResearchPlan`) | `investment_debate_state.judge_decision`, `investment_plan` |
| Trader | `create_trader` | quick | yes (`TraderProposal`) | `trader_investment_plan` |
| Risk Debators ×3 | `create_aggressive/conservative/neutral_debator` | quick | no (prose) | `risk_debate_state` |
| Portfolio Manager | `create_portfolio_manager` | deep | yes (`PortfolioDecision`) | `risk_debate_state.judge_decision`, `final_trade_decision` |

Patterns worth reusing:

- **Analysts** are tool-calling loops: `prompt | llm.bind_tools(tools)` with a
  conditional edge back to a LangGraph `ToolNode` until the model stops
  emitting tool calls (`conditional_logic.should_continue_<analyst>`).
- **Decision agents** follow the canonical structured-output pattern in
  `agents/utils/structured.py`: bind schema once, invoke, render to markdown;
  on any failure fall back to free text so the pipeline never blocks.
- The **Market Analyst** is grounded by a deterministic verification snapshot
  (`get_verified_market_snapshot`) that it must treat as source of truth for
  exact numbers — an anti-hallucination mechanism (#830).
- Instrument identity is resolved **once** per run (`resolve_instrument_identity`,
  cached) and injected into every agent prompt (`instrument_context`).

---

## 4. CURRENT — Data Flow

1. User supplies ticker (any Yahoo-style or broker-style symbol) + date.
2. `normalize_symbol()` maps broker syntax → Yahoo symbol
   (purely syntactic, no network): `XAUUSD→GC=F`, `XAGUSD→SI=F`,
   `BTCUSD→BTC-USD`, `EURUSD→EURUSD=X`, `SPX500→^GSPC`.
3. `resolve_instrument_identity()` fetches name/sector/exchange from yfinance
   (fail-open `{}`), rendered into `instrument_context`.
4. During analysis, agents pull data through `route_to_vendor(method, ...)`
   which dispatches to the configured vendor chain (explicit config only —
   no silent fallback to unchosen vendors). Failures surface as:
   - typed `NoMarketDataError` → `NO_DATA_AVAILABLE:` sentinel text,
   - `VendorRateLimitError` / `VendorNotConfiguredError` → next vendor in chain,
   - optional categories (`macro_data`, `prediction_markets`) degrade to a
     `DATA_UNAVAILABLE:` sentinel instead of aborting the run.
5. OHLCV is cached under `data_cache_dir` with a same-day TTL (900 s) and a
   staleness guard (latest row ≤ 10 calendar days old), look-ahead filtered.
6. Indicators come from `stockstats` (`get_stock_stats_indicators_window`)
   or Alpha Vantage; exact values cross-checked against the verified snapshot.

**Granularity limitation:** the whole pipeline is daily-bar oriented.
`trade_date` is a `%Y-%m-%d` string everywhere; there is no timeframe concept,
no timezone-aware timestamps, and no intraday intervals in state, tools,
cache keys, or news windows.

---

## 5. CURRENT — State Management

`AgentState` (LangGraph `MessagesState` TypedDict in
`agents/utils/agent_states.py`) carries:

- identity: `company_of_interest`, `asset_type` ("stock"|"crypto"),
  `instrument_context`, `trade_date`
- analyst reports: `market_report`, `sentiment_report`, `news_report`,
  `fundamentals_report`
- debates: `investment_debate_state` (bull/bear history, judge),
  `risk_debate_state` (aggressive/conservative/neutral, judge)
- decisions: `investment_plan`, `trader_investment_plan`,
  `final_trade_decision`, `past_context` (memory-log injection)

Optional SQLite checkpointing (`--checkpoint` / `checkpoint_enabled`)
persists per-node state keyed by ticker+date+graph-shape signature so crashed
runs resume; cleared on success.

---

## 6. CURRENT — Model/Provider Layer

`llm_clients.factory.create_llm_client(provider, model, base_url)` returns a
`BaseLLMClient`. Two native paths (anthropic, google) plus azure/bedrock
wrappers; everything else routes through the OpenAI-compatible client backed
by a provider registry (openai, deepseek, xai, qwen/dashscope intl+CN, zhipu
intl+CN, minimax intl+CN, openrouter, groq, mistral, moonshot, nvidia, ollama,
openai_compatible). Cross-cutting knobs forwarded when set: `temperature`,
`llm_max_retries`, provider thinking/reasoning controls. Model catalog +
capability flags live alongside. API-key auto-detection selects a provider
when none is configured.

---

## 7. CURRENT — Tool Layer

LangChain `@tool` wrappers (one file per category) all funnel into
`dataflows.interface.route_to_vendor`. Registry (`VENDOR_METHODS`):

| Method | Vendors |
|---|---|
| `get_stock_data` | yfinance, alpha_vantage |
| `get_indicators` | yfinance (stockstats), alpha_vantage |
| `get_fundamentals` / `get_balance_sheet` / `get_cashflow` / `get_income_statement` | yfinance, alpha_vantage |
| `get_news` / `get_global_news` / `get_insider_transactions` | yfinance, alpha_vantage |
| `get_macro_indicators` | fred (needs key) |
| `get_prediction_markets` | polymarket (keyless) |

Vendor selection: `config["data_vendors"]` per category, overridden by
`config["tool_vendors"]` per method; comma-separated chains tried in order.

Sentiment data (StockTwits + Reddit) is fetched directly by the Sentiment
Analyst node (`fetch_stocktwits_messages`, `fetch_reddit_posts`), not via the
vendor router, and feeds the structured `SentimentReport`.

---

## 8. CURRENT — Configuration

- `default_config.DEFAULT_CONFIG`: single dict (paths, LLM provider/models,
  debate rounds, news limits, vendor map, benchmark map).
- `TRADINGAGENTS_*` env overrides applied at import time via `_ENV_OVERRIDES`
  table with type-coercion against existing defaults (invalid values fail loudly).
- Global runtime access via `dataflows.config.get_config()/set_config()`.
- `.env.example` documents every supported variable.

## 9. CURRENT — External APIs

| Provider | Used for | Auth |
|---|---|---|
| Yahoo Finance (yfinance lib, unofficial) | OHLCV, fundamentals, news, identity, benchmarks | none |
| Alpha Vantage | optional fallback for stock/indicators/fundamentals/news | `ALPHA_VANTAGE_API_KEY` |
| FRED | macro series (rates/inflation/labor/growth) | `FRED_API_KEY` |
| Polymarket Gamma API | event probabilities | none |
| StockTwits public API | sentiment messages | none |
| Reddit public JSON | sentiment posts | none |
| LLM providers | reasoning | per-provider key (see ACCESS_INFO.md) |

Details incl. rate limits and failure behavior: see `API_DOCUMENTATION.md`.

## 10. CURRENT — Error Handling

- Typed vendor-error taxonomy routed *by behavior* (see §4); no silent
  fallbacks; broken primaries logged even when a fallback succeeds.
- Optional enrichment categories degrade to sentinel strings.
- Structured-output agents retry once as free text before giving up.
- yfinance calls retry on rate limits with exponential backoff; same-day
  cache prevents refetch storms.
- Known gap: an unexpected exception inside one agent node still fails the
  whole graph invocation; there is no per-node failure record in state.

## 11. CURRENT — Logging & Observability

Standard-library `logging` per module (INFO/WARNING); rich console output in
the CLI; JSON full-state logs and markdown report trees on disk; decision
memory log. No unified structured run log correlating asset/timeframe/data
timestamps/agent failures yet.

## 12. CURRENT — Testing Structure

- pytest, markers `unit` / `integration` / `smoke`; CI matrix py3.10–3.13.
- All tests run offline: conftest injects placeholder API keys and resets the
  global dataflows config between tests.
- Coverage highlights: symbol normalization paths, vendor routing/errors,
  OHLCV cache freshness + stale guards, date boundaries/look-ahead, crypto
  asset mode, structured agents/prompts, checkpoint resume, CLI precedence,
  LLM provider knobs, news windows, i18n.
- `ruff check .` strict and clean (line-length exempt E501).

---

# PHASE 1 — Research Platform (IMPLEMENTED)

Goal: a research-only signal engine for `XAUUSD` and `BTCUSD` that reuses the
existing stack and adds the missing abstractions: asset registry, timeframes,
provider interface with explicit data status, normalized OHLCV model,
structured research reports/signals, deterministic confidence, and graceful
degradation. **No order execution anywhere.**

Implementation status: all P1.1–P1.8 components below exist in the tree with
unit/integration tests (`tests/test_asset_registry.py`,
`test_timeframes.py`, `test_marketdata_models.py`, `test_yahoo_provider.py`,
`test_indicators_engine.py`, `test_confidence.py`, `test_research_schemas.py`,
`test_signal_assembly.py`, `test_research_logging.py`,
`test_research_engine.py`) plus CLI coverage in
`tests/test_cli_no_console.py`. Entry point:
`python cli/main.py research --asset XAUUSD --timeframe 1h [--save DIR]`.

Two deliberate deviations from the original proposal, both recorded here:

- The indicator engine computes RSI/MACD/SMA/EMA/Bollinger/ATR with explicit
  pandas expressions instead of stockstats, because stockstats' column-naming
  convention (`close_50_sma`) cannot express our non-default windows
  (ema_10, sma_50/200, boll_20±2σ) cleanly; explicit code keeps every window
  visible and deterministic.
- Bare invocation of the CLI still runs the legacy interactive flow: a root
  callback (`cli/main.py::_root_fallback`) restores single-command behavior
  after the second subcommand switched typer into multi-command mode.

## P1.1 New package layout (additive)

```
tradingagents/
├── assets/
│   └── registry.py        # NEW: AssetSpec registry (canonical IDs XAUUSD/BTCUSD)
├── marketdata/            # NEW: normalized market-data layer
│   ├── models.py          # Bar/OHLCVSeries/Quote models w/ DataStatus enum
│   ├── timeframes.py      # Timeframe enum (15m, 1h, 4h, 1d)
│   ├── provider.py        # MarketDataProvider Protocol/ABC
│   ├── yahoo_provider.py  # adapter over existing dataflows/y_finance functions
│   └── errors.py          # thin reuse/wrap of dataflows.errors
├── analysis/
│   ├── indicators.py      # deterministic indicator engine (stockstats-backed)
│   └── confidence.py      # documented heuristic aggregation
├── research/
│   ├── schemas.py         # TechnicalAnalysis, NewsAnalysis, BullCase, BearCase,
│   │                      # ResearchReport, ResearchSignal (+ render helpers)
│   ├── engine.py          # ResearchEngine orchestrating providers+graph+assembly
│   └── logging.py         # structured run log (stdlib logging, JSON formatter)
└── (existing packages unchanged)
```

## P1.2 Asset registry

Central `AssetSpec` records: `asset_id` (`XAUUSD`, `BTCUSD`), display name,
asset class, quote currency, provider-symbol mapping (Yahoo `GC=F` /
`BTC-USD`), tradable-hours hint, benchmark/macro context hints. Agents receive
symbols only via the registry; adding EURUSD/ETHUSD/NASDAQ later = new rows,
not engine edits. Reuses (does not duplicate) `symbol_utils.normalize_symbol`
for any free-text input mapping.

## P1.3 Market-data abstraction

```python
class MarketDataProvider(Protocol):
    def get_ohlcv(self, asset: AssetSpec, timeframe: Timeframe,
                  *, limit: int | None = None,
                  start=None, end=None) -> OhlcvSeries: ...
    def get_quote(self, asset: AssetSpec) -> Quote | None: ...
```

- First implementation `YahooMarketDataProvider` wraps the existing
  `y_finance` functions (reuse, not rewrite); `AlphaVantageMarketDataProvider`
  can slot in later behind the same interface.
- Every returned series/quote carries `source`, `timeframe`,
  `DataStatus` ∈ {REALTIME, DELAYED, HISTORICAL, CACHED, SIMULATED}, and
  tz-aware UTC timestamps. Gold via `GC=F` ⇒ DELAYED (futures quotes);
  BTC-USD ⇒ REALTIME-ish (labelled per provider metadata).
- No silent mixing of statuses/timeframes inside a series; the engine refuses
  to produce a signal from stale data (staleness thresholds per timeframe).

## P1.4 Deterministic technical layer

`analysis/indicators.py` computes RSI, MACD, SMA20/50/200, EMA10, Bollinger
Bands, ATR, momentum, volatility over the normalized series (stockstats
backend already used repo-wide). Output = structured dict/pydantic fragment,
not markdown. This becomes ground truth injected into agent prompts (same
anti-hallucination role as today's verified snapshot).

## P1.5 Research workflow

Reuse `TradingAgentsGraph`'s node pattern but assemble a dedicated
research-oriented graph in `research/engine.py`:

```
Technical Analyst → Macro/Crypto-Context Analyst → News Analyst →
Sentiment Analyst → Bull Researcher ⇄ Bear Researcher →
Research Manager → Signal Assembler (deterministic)
```

- Analysts consume the precomputed indicator block + provider data (fewer
  speculative tool loops; tools remain available where useful) and emit
  **structured** section schemas (new Pydantic models in
  `research/schemas.py`, following the existing `schemas.py` +
  `structured.py` pattern — not a second schema system).
- Failure policy per node: catch, record `status=unavailable` + reason in the
  report, continue. Missing sections are visible, never fabricated.
- Macro analyst uses FRED data when configured; otherwise marks itself
  unavailable (never invents rates).

## P1.6 Structured outputs

`ResearchReport` (all sections + per-section status/source/timestamps) and
`ResearchSignal` (asset, timestamp, timeframe, action BUY/SELL/HOLD,
confidence, entry/stop/target references, risk level, thesis, supporting/
opposing factors, invalidation conditions, data sources, model used) with a
mandatory `RESEARCH SIGNAL — NOT EXECUTED` label in every rendering. Levels
are reference levels derived deterministically from verified bars/ATR, not
free-invented LLM numbers; LLM-proposed levels must be within tolerance of
verified data or are flagged.

## P1.7 Confidence (documented heuristic, experimental)

Deterministic aggregation, weights published in code + docs:
agent directional agreement (analysts + bull/bear verdicts), data completeness
(status of each source), signal consistency (action vs trend/vol regime),
and model self-reported confidence where present. Clearly labelled heuristic —
not a statistically validated measure.

## P1.8 Entry points

- Python: `ResearchEngine(config).run(asset_id, timeframe)` →
  `(ResearchReport, ResearchSignal)`.
- CLI: new subcommand `research` alongside existing `analyze` (which stays
  byte-compatible).

## P1.9 Explicit non-goals (Phase 1)

No live trading, no broker connectivity, no order objects, no auth/billing/
dashboard/SaaS. Interfaces kept clean so Phase 3 (paper trading) can consume
`ResearchSignal` without modification.

---

# PHASE 2 — Backtesting Engine (IMPLEMENTED)

Goal: historical research and strategy evaluation on top of Phase 1 —
**one research/signal engine, multiple execution environments**. The
backtester replays the *same* `ResearchEngine` over a deterministic
historical timeline and evaluates the resulting `ResearchSignal` stream with
a simulated executor. **Still research-only**: nothing here places orders or
connects to a broker; "execution" means deterministic simulation only.
Status: **implemented and tested** (`tradingagents/backtest/`, CLI command
`backtest`, tests `tests/test_backtest_*.py`). Documented deviations from
the original design are noted inline below.

## P2.0 Core principle — no duplicated signal logic

The backtester must not reimplement signal generation. It reuses:

- `MarketDataProvider` protocol (new `ReplayMarketDataProvider`
  implementation serves stored bars up to simulation time),
- `OhlcvSeries`/`Bar`/`Quote` models unchanged,
- asset registry + `Timeframe` enum unchanged,
- `compute_indicators` / `TechnicalSnapshot` unchanged (pure functions of a
  truncated series — recomputed per decision step on data available at T),
- all seven research agents + `ResearchSignal` schemas unchanged,
- `DEFAULT_CONFIG`, `log_event`, vendor error taxonomy unchanged.

## P2.1 The one required engine change — injectable clock

`ResearchEngine` currently calls `datetime.now(timezone.utc)` directly
(freshness gate, timestamps, news windows). Phase 2 adds an optional
constructor parameter `now_fn: Callable[[], datetime]` defaulting to real
UTC now — zero behavior change for existing callers. The backtester injects
a `SimulationClock` so "current time" is the simulated timestamp T. This is
the single smallest change that makes the live research path and the
backtest path literally the same code.

## P2.2 New package layout (additive): `tradingagents/backtest/`

```
tradingagents/backtest/
├── __init__.py
├── clock.py            # SimulationClock: deterministic sequential UTC clock
├── historical/
│   ├── store.py        # HistoricalDataStore Protocol + JsonDataStore impl
│   ├── yahoo_history.py# fetch+persist history via existing Yahoo adapter
│   ├── replay_provider.py # MarketDataProvider over the store at time T
│   └── validation.py   # deterministic dataset quality report (reject/flag)
├── execution.py        # ExecutionSimulator (next-bar-open entry, SL/TP)
├── portfolio.py        # cash/equity/exposure/drawdown accounting
├── ledger.py           # TradeRecord schema + JSON/CSV export
├── analytics.py        # performance stats (N/A when insufficient obs)
├── baselines.py        # buy&hold, SMA crossover, momentum
├── walkforward.py      # train/validation/test window generator + aggregation
├── research_cache.py   # reuse of identical research runs (documented keys)
├── report.py           # BacktestReport schema, run_id, provenance metadata
└── cli.py              # register_backtest_command(app) → cli/main.py
```

Layer position: `backtest/` may import `marketdata/`, `assets/`,
`analysis/`, `research/`, `dataflows/`; nothing in lower layers imports
`backtest/`.

## P2.3 Historical data

- **Source**: Yahoo Finance via the *existing* yfinance stack
  (`yf_retry`, same symbol mapping from the registry). Known constraint to
  document: Yahoo intraday depth is bounded (~60 days for 15m, ~730 days for
  1h); daily goes back decades. The store records exactly what was fetched.
- **Storage**: one JSON dataset file per (asset, timeframe, source, range)
  under `DEFAULT_CONFIG["data_cache_dir"]/historical/`, plus a metadata
  sidecar recording source, symbol mapping, range, fetch timestamp, bar
  count, timezone (UTC everywhere), and a content-hash dataset id used as
  filename. Behind the `HistoricalDataStore` Protocol so Parquet/DuckDB can
  be added later without touching engine/backtester.
- **Validation** (`validation.py`): deterministic checks — ordering,
  duplicates, missing expected candles (gap detection per timeframe calendar
  approximation), OHLC relationships, non-finite/negative prices, tz-awareness.
  Policy: invalid rows are **rejected with a typed error listing reasons**;
  gaps are flagged in metadata, never silently interpolated.
- **Provenance**: every backtest report embeds the dataset ids it consumed.

## P2.4 Information-availability rules (anti look-ahead)

1. A bar stamped T carries information available **at its close**; decision
   timestamps are bar-close timestamps.
2. At decision time T the replay provider returns only bars with
   `timestamp <= T`; indicators are recomputed on that truncated series.
3. Signals execute at the **next bar's open after the decision close**
   (default; configurable delay in bars), never at the decision candle's own
   close/open.
4. News/sentiment are **disabled by default** in backtests (live-only
   endpoints would fabricate recency). Reports state enabled components
   explicitly. FRED macro uses `curr_date=T` (genuinely historical).
5. Stop/target fills resolve intra-candle conservatively: if both SL and TP
   fall inside one bar, the **stop wins** (pessimistic assumption).
6. Walk-forward windows slice by the same timestamp semantics; test-window
   data is invisible to training/research phases.

## P2.5 Simulation, portfolio, analytics

- Deterministic `ExecutionSimulator`: market entries/exits, SL/TP,
  configurable `slippage_bps`, per-asset `spread_bps`, commission per side;
  fixed-notional or pct-of-equity sizing (LLM never sizes positions);
  risk limits (max position, max simultaneous positions, max exposure).
- `portfolio.py` tracks cash, open positions, realized/unrealized PnL,
  equity, exposure, drawdown per bar → structured equity curve rows.
- `ledger.py`: one immutable record per trade (ids, timestamps, prices,
  costs, gross/net PnL, return %, duration, exit reason, strategy version);
  JSON + CSV export.
- `analytics.py`: returns/trade stats/risk/risk-adjusted/profitability/
  behavior metrics; anything statistically meaningless at low observation
  counts returns `"N/A"` with the reason. Buy&Hold benchmark per asset.
- `report.py`: BACKTEST REPORT (JSON-first, human rendering second) with
  run_id (uuid + config hash), git commit, strategy/dataset/model versions,
  full cost & assumption disclosure. Disclaimer: past simulation ≠ future
  results.

## P2.6 Walk-forward + AI usage tracking

- `walkforward.py`: configurable train/validation/test windows + step +
  minimum-bars guard; per-window independent results; aggregation
  (mean/median/worst/best/% profitable) only after all windows complete;
  basic overfitting diagnostics reported as *evidence* (train/test gap,
  window instability, trade-count floor), never as claims of absence.
- `research_cache.py`: key = hash(asset, T, timeframe, input-data hash,
  model ids, prompt/config version) → cached `ResearchSignal` JSON.
  Invalidation = any key-component change. Cache hits are recorded in the
  report for reproducibility. LLM call counts (+token usage when the
  provider exposes it; costs only when configured pricing exists — never
  invented) tracked per run.

## P2.7 Explicit non-goals (Phase 2)

No broker connectivity, no optimization/search loops (only the fixed
strategy set), no dashboard UI, no multi-process parallelism beyond safe
per-window isolation, no fabricated historical news/sentiment.

## P2.8 As-built notes (deviations from the original design)

- CLI wiring lives in `cli/backtest.py` (mirrors `cli/research.py`), not a
  package-internal `backtest/cli.py`; the orchestrator itself is
  `tradingagents/backtest/engine.py` (`run_backtest`, `run_walk_forward`,
  `AIResearchStrategy`, `_CountingLLM`, `BacktestRunOutput`).
- `ResearchEngine` additionally accepts `disabled_components` (beyond
  `now_fn`) so news/sentiment/macro can be switched off structurally —
  disabled gatherers append an explicit `DataSourceRef(status="unavailable")`
  instead of silently skipping.
- Dataset metadata is embedded **inside** the dataset JSON payload (one
  file, atomic write, content-hash integrity check on load) rather than a
  separate sidecar file; corrupt/unreadable datasets raise
  `HistoricalDataError` uniformly.
- Walk-forward currently runs **out-of-sample-only** windows: decisions are
  confined to each frame's test phase while earlier bars serve as indicator
  context — equivalent to live operation for the parameter-free baselines.
  When parameterized strategies arrive, fitting must happen inside
  train/validation phases only (documented in `run_walk_forward`).
- Portfolio accounting is settled-cash margin style: `cash` changes only on
  position close; unrealized P&L marks separately. No financing costs or
  margin calls (documented simplification; never affects signal logic).
- Each strategy pass gets its own `SimulationClock`/provider instance
  (the clock forbids rewinding, so passes cannot share one timeline).
- Equity curves are anchored at configured capital before the first trade;
  annualized return degrades to `N/A` with a reason when the window is too
  short to annualize meaningfully (overflow guard).
- Pending fills count *bars*, not wall time, so weekend/holiday gaps can
  never cause phantom time-based executions.

---

# PHASE 3 — Paper Trading (IMPLEMENTED)

Goal: a **real-time paper-trading environment** that reuses the Phase 1
research engine and Phase 2 execution/portfolio/ledger/analytics stack
unchanged, drives them with *actual current market time*, and persists every
artifact (signals, orders, positions, trades, journal, equity curve) so the
system survives restarts. **Still 100% simulated**: no broker connectivity,
no order-placement code paths, no live-account credentials — the only
executor in the system remains `backtest.execution.ExecutionSimulator`
(pure arithmetic). Future `BrokerExecution` (Phase 5) must be a *separate*
class behind an execution interface; nothing in `tradingagents/paper/` may
import or reference broker SDKs (enforced by a forbidden-import test,
`tests/test_paper_safety.py`).
Status: implemented (`tradingagents/paper/`, `cli/paper.py`); offline-tested.

## P3.0 Core principle — no duplicated signal logic

The paper engine is an orchestrator, not a second brain. One cycle
(`PaperTradingEngine.run_cycle`) follows this exact order:

```
guards (kill switch → halt flag → schedule lookup)
→ fetch bars → drop unclosed tail (effective close = stamp + interval)
→ novelty gate (skip unless a new bar closed since last processed)
→ reconstruct account + ledger from store; restore ACCEPTED intents into
  ExecutionSimulator → replay every newly closed bar through
  on_bar_open/on_bar_close (fills at next-bar open; SL/TP resolved honestly,
  stop wins ties — including bars missed while the process was down)
→ persist accounting (positions, account, equity snapshot, daily rollup,
  scheduler bookkeeping) exactly once per consumed slot-cycle
→ content-based duplicate suppression (BEFORE any LLM spend)
→ ResearchEngine (Phase 1, unchanged) → persist GENERATED signal record
→ HOLD ⇒ hold_no_trade → validation gate → RiskEngine vetoes
→ accept: signal/order → ACCEPTED, intent queued for the NEXT bar's open
→ structured events throughout; a cycle never raises into the loop
  (`cycle_failed` is a returned status with logged error detail)
```

As-built notes (deviations/refinements vs. the original sketch):
- Duplicate suppression uses the content-based `signal_id` (below), not
  `ResearchCache`: identical market state ⇒ identical id ⇒ suppressed before
  research runs, so restarts and retries can never double-spend or
  double-execute.
- Fresh slots never backfill history: the first cycle on a new schedule
  analyses only the newest closed bar and records its effective close as the
  novelty watermark.
- Pending intents are persisted as ACCEPTED order events and re-armed into
  the simulator on every cycle until filled or expired; an intent whose slot
  loses its schedule entry (or is superseded by a fresh decision) is marked
  EXPIRED deterministically.
- An equity snapshot is appended once per consumed slot-cycle (including
  hold/duplicate/retry outcomes) so the curve reflects monitoring reality,
  not just trades.
- The AI never sizes positions and cannot remove stops: sizing comes from
  `SizingPolicy`; a directional signal without a stop level is rejected by
  both validator (`missing_stop_level`) and risk engine.

Reused verbatim: `ResearchEngine`, `ResearchSignal` schemas, asset registry,
`Timeframe` staleness windows, `ExecutionConfig/SizingPolicy/RiskLimits`,
`ExecutionSimulator`, `Portfolio/Position/size_position`, `TradeLedger/
TradeRecord`, `analytics.compute_stats` (+N/A-with-reason policy),
`bars_content_hash` (Phase 2 dataset hashing), `log_event`. New code only
adds: identity/idempotency, validation, risk vetoes, persistence,
scheduling, reporting.

## P3.1 Package layout (additive): `tradingagents/paper/`

```
tradingagents/paper/
├── config.py        # PaperTradingConfig: enabled kill-switch, environment
│                    #   Literal["test","paper"] (no "live" value exists),
│                    #   account_id, initial_capital, per-timeframe stale
│                    #   overrides, risk limits (extends RiskLimits fields)
├── models.py        # PaperOrder lifecycle (SIGNAL→PENDING→ACCEPTED→EXECUTED
│                    #   →OPEN→CLOSED; REJECTED/EXPIRED/CANCELLED/FAILED),
│                    #   PaperSignalRecord (state GENERATED/ACCEPTED/REJECTED
│                    #   /EXECUTED/EXPIRED/SUPERSEDED + reason), EquitySnapshot,
│                    #   DailyPerformanceRow, JournalEntry (+human notes field)
├── signal_id.py     # deterministic idempotent id = sha256(asset|timeframe|
│                    #   decision_at|visible_bars_hash|PROMPT_VERSION|
│                    #   config_hash)[:16] — mirrors ResearchCache key so the
│                    #   same market state always yields the same signal id
├── validator.py     # pre-execution gate: supported asset/timeframe, sane
│                    #   timestamps, freshness vs staleness_hours, price/
│                    #   SL/TP logical validity vs action side, duplicates;
│                    #   invalid ⇒ rejected + logged, never executed
├── risk.py          # RiskEngine.veto(): ordered deterministic checks — kill
│                    #   switch/halt flag, max daily loss, max drawdown, max
│                    #   simultaneous positions, max total exposure, max risk
│                    #   per trade (|entry−stop|·qty ≤ pct·equity), position
│                    #   notional cap; first failure wins, all logged
├── store.py         # PaperStateStore Protocol + JsonPaperStateStore:
│                    #   {data_cache_dir}/paper/{env}/{account_id}/ with
│                    #   atomic writes (tmp+rename), pydantic-validated loads,
│                    #   append-only orders.jsonl / trades.jsonl /
│                    #   equity_curve.jsonl / daily.jsonl / signals index,
│                    #   per-signal JSON, journal/, scheduler.json,
│                    #   state.json; corrupt state raises PaperStateError
│                    #   loudly (never silently reset)
├── performance.py   # live stats reusing compute_stats + daily/weekly/monthly
│                    #   P&L aggregation + N/A-with-reason policy
├── report.py        # PAPER_ACCOUNT_REPORT schema/text render; disclaimer
│                    #   "PAPER — SIMULATED EXECUTION"
├── events.py        # event-name constants + NotificationProvider Protocol
│                    #   with LoggingNotificationProvider default (no framework)
├── scheduler.py     # pure schedule math: entries {asset,timeframe,enabled},
│                    #   next_run from last processed bar timestamp (persisted);
│                    #   skips cycles when no new closed bar exists
├── engine.py        # PaperTradingEngine.run_cycle(asset, timeframe):
│                    #   guards → fetch → closed-slice → novelty gate →
│                    #   reconstruct + replay bars closed since last processed
│                    #   bar through on_bar_open/on_bar_close (SL/TP resolved
│                    #   honestly at bar level after downtime) → mark-to-market
│                    #   → snapshots → persist → dedupe → research → validate
│                    #   → risk → accept; read APIs: account_summary() and
│                    #   build_report() (Phase 4 consumes these unchanged)
└── (CLI lives at the top-level cli/paper.py, registered into cli/main.py)
```

Layer position: `paper/` may import `marketdata/`, `assets/`, `analysis/`,
`research/`, `dataflows/`, `backtest/`; nothing lower imports `paper/`.

## P3.2 Safety isolation (absolute requirement)

- No broker SDK may appear in any dependency used by `tradingagents/paper/`;
  a dedicated test fails CI if forbidden module names (`ccxt`, `alpaca`,
  `ib_insync`, `binance`, `oanda`, `mt5`, …) are imported anywhere under
  `tradingagents/paper/`.
- Two switches must both pass before any cycle runs:
  `PaperTradingConfig.enabled` **and** absence of a persisted emergency-halt
  flag (`paper halt` / `resume` commands). Halted state is reported loudly.
- Environment is `Literal["test","paper"]` — a `"live"` value cannot be
  constructed. TEST mode uses fake providers labelled
  `DataStatus.SIMULATED`; PAPER mode uses real Yahoo data labelled by
  `classify_status`.
- Output labels change from "NOT EXECUTED" to "PAPER — SIMULATED EXECUTION";
  PROJECT_RULES §24 documents this label category (amendment approved
  2026-08-24; carried by `PAPER_DISCLAIMER` and every report render).

## P3.3 Live-time differences handled honestly

- Decisions and fills happen on **closed-bar boundaries** of real fetched
  data: entry fills at the open of the first bar closing *after* the signal
  (`entry_delay_bars=1` semantics preserved); between-cycle SL/TP triggers
  are resolved by replaying every bar closed since the last processed bar —
  after downtime the stop is applied at the honest historical bar, never at
  a fabricated "current" price.
- Freshness gate reuses `Timeframe.staleness_hours()` with optional tighter
  per-timeframe config overrides; stale ⇒ cycle aborts with
  `SIGNAL BLOCKED — market data stale` (logged, persisted as REJECTED).
- No synthetic fallback prices; provider errors/timeouts mark the cycle
  failed (`FAILED` order state where applicable) and are retried next due
  slot, never silently skipped.
- LLM/agent failures follow Phase 1 semantics: failures recorded; if
  insufficient research remains, **no signal is generated** (reason stored).

## P3.4 Persistence & recovery

Layout under `{data_cache_dir}/paper/{environment}/{account_id}/`:
`state.json` (cash/realized/costs/counters/halt flag/schema_version),
`positions.json`, `orders.jsonl` (append-only transitions),
`signals/{signal_id}.json` + `signals_index.jsonl`, `trades.jsonl`,
`journal/{trade_id}.json` (embedded exact research snapshot + notes),
`equity_curve.jsonl`, `daily.jsonl`, `scheduler.json`. All writes atomic;
all loads schema-validated; corruption raises instead of resetting.
Startup recovery: load account → positions → pending orders → validate →
reconcile against fresh bars (resolve SL/TP over missed bars, mark pending
fills superseded if already open) → resume; every step logged
(`paper_recovery_*` events). Multiple accounts supported by construction
(state paths keyed by `account_id`; no singletons).

## P3.5 Attribution & analytics

Trade → Order → signal_id → ResearchSignal snapshot (thesis/bull/bear/
factors/confidence/models/data sources) → dataset bar hash: full chain
persisted at creation time, never regenerated later. Signal-vs-trade
analytics count GENERATED/ACCEPTED/EXECUTED/REJECTED/EXPIRED/SUPERSEDED and
evaluate what-if performance of risk-rejected signals on the subsequent
price path (deterministic offline analyzer).

## P3.6 Explicit non-goals (Phase 3)

No dashboard UI (Phase 4 consumes the JSONL artifacts via clean read APIs),
no broker execution class (Phase 5), no background daemon inside library
code (CLI loop wrapper only), no notification providers beyond logging
(Protocol ready for Telegram/Discord later), no new third-party
dependencies (stdlib scheduler + JSON persistence).

---

## Future Phase Boundaries (documentation only — do not implement)

| Phase | Scope | Boundary hooks available |
|---|---|---|
| 2 Backtesting | replay signals over history | **implemented** (`tradingagents/backtest/`, P2.x above) |
| 3 Paper Trading | simulated fills of signals | **implemented** (`tradingagents/paper/`, `cli/paper.py`, P3.x above); reuses backtest executor/portfolio verbatim |
| 4 Dashboard | UI over stored runs | report tree + structured JSON artifacts + equity curve rows |
| 5 Broker Integration | real execution | out of scope; nothing in core may import brokers |
| 6 SaaS/API | hosted service | engine is a pure-Python entry point |

---

*Last verified against repository state: 2026-08-24 (v0.3.1 + Phase 1 +
Phase 2 + Phase 3 implementation; suite 878 passed / 2 skipped / 69 subtests,
ruff clean). Update this file
whenever implementation changes architecture.*
