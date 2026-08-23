# API_DOCUMENTATION.md

Documents every external data/LLM provider the project uses and the internal
interfaces that become stable contracts. No secrets appear here — see
`ACCESS_INFO.md` for credential setup. Status: verified against v0.3.1
(2026-08-23); Phase 1 additions (Part C) are implemented and tested.

---

## Part A — External Providers (CURRENT)

### A1. Yahoo Finance (via `yfinance` library, unofficial API)

| Property | Value |
|---|---|
| Purpose | Primary vendor: OHLCV bars, technical indicators input, fundamentals, news, instrument identity, benchmark returns |
| Endpoint/library | `yfinance` Python package (`yf.Ticker`, `yf.download`) |
| Auth | None |
| Required env vars | None |
| Rate limits | Unofficial/undocumented; HTTP 429s observed under burst use → `yf_retry` exponential backoff (`dataflows/stockstats_utils.py`) |
| Data returned | OHLCV DataFrames; company info dict; news lists; insider transactions |
| Symbols supported | Any Yahoo symbol; broker syntax normalized by `normalize_symbol()` (XAUUSD→GC=F, BTCUSD→BTC-USD, EURUSD→EURUSD=X, SPX500→^GSPC) |
| Timeframes | Daily bars in current pipeline (intraday intervals exist in yfinance but are unused) |
| Failure behavior | Empty/stale frames raise `NoMarketDataError`; 429 retried then propagated; router falls to next configured vendor if any |
| Caching | OHLCV cached under `data_cache_dir`, same-day TTL 900 s, staleness guard ≤10 calendar days, look-ahead rows filtered |
| Data status | Delayed for exchange quotes (typically ~15 min; futures GC=F likewise); EOD after close. **Treated as historical/daily throughout current code** |

### A2. Alpha Vantage (optional fallback)

| Property | Value |
|---|---|
| Purpose | Optional alternate vendor for stock data, indicators, fundamentals, news |
| Endpoint/library | REST via requests (`tradingagents/dataflows/alpha_vantage*.py`) |
| Auth | API key (`ALPHA_VANTAGE_API_KEY`), `apikey` query param |
| Rate limits | Free tier: 25 requests/day, 5/min (premium higher). Handled via `VendorRateLimitError` |
| Data returned | JSON time series, indicator series (RSI/MACD/SMA…), overview/fundamentals, news & sentiment |
| Symbols | US-listed equities/ETFs primarily; limited FX/crypto |
| Failure behavior | Missing key → `VendorNotConfiguredError`; rate-limit/throttle → `VendorRateLimitError`; empty → `NoMarketDataError`. Look-ahead-filtered fundamentals (#3570f2e) |
| Caching | None dedicated (router-level only) |
| Data status | End-of-day / delayed depending on endpoint |

### A3. FRED (Federal Reserve Economic Data)

| Property | Value |
|---|---|
| Purpose | Macro context: interest rates, inflation, labor, growth |
| Endpoint/library | FRED REST API via requests (`dataflows/fred.py`) |
| Auth | `FRED_API_KEY` (`api_key` param) |
| Rate limits | Generous; undocumented hard caps |
| Data returned | Series observations rendered as text block for macro analyst |
| Coverage | US macro series (DFF, CPIAUCSL, UNRATE, GDP growth, etc.) |
| Failure behavior | Missing key → `VendorNotConfiguredError` → optional-category sentinel (`DATA_UNAVAILABLE:`), run continues |
| Caching | None dedicated |
| Data status | Historical/EOD official statistics |

### A4. Polymarket Gamma API

| Property | Value |
|---|---|
| Purpose | Market-implied probabilities for forward-looking events |
| Endpoint/library | Public REST (`dataflows/polymarket.py`) |
| Auth | None (keyless) |
| Rate limits | Undocumented public limits |
| Data returned | Event/question probabilities as text |
| Failure behavior | Errors degrade via optional-category sentinel; never abort a run |
| Data status | Real-time market prices |

### A5. StockTwits public API + Reddit public JSON (sentiment)

| Property | Value |
|---|---|
| Purpose | Retail sentiment messages/posts per ticker |
| Library | requests (`dataflows/stocktwits.py`, `dataflows/reddit.py`) |
| Auth | None |
| Symbols | StockTwits uses mapped symbols (crypto mapped #a102afa); Reddit searches ticker communities |
| Failure behavior | Resilience-tested (`test_stocktwits_resilience.py`, `test_reddit_fallback.py`); sentiment analyst degrades with low-confidence band |
| Data status | Real-time posts; timestamps preserved where provided |

### A6. LLM Providers (reasoning layer)

Providers via `llm_clients.factory.create_llm_client`: openai, anthropic,
google, azure, bedrock (+API-key bearer auth), ollama, deepseek, xai, qwen
(dashscope intl/CN), zhipu (intl/CN), minimax (intl/CN), openrouter, groq,
mistral, moonshot, nvidia, and any OpenAI-compatible endpoint
(`openai_compatible`). Structured output used where supported
(json_schema / response_schema / tool-use); free-text fallback otherwise.
Keys required per provider (see ACCESS_INFO.md). Failures surface as
exceptions inside agent nodes; decision agents retry once as free text.

---

## Part B — Internal Stable Interfaces (CURRENT)

### B1. `TradingAgentsGraph` (`graph/trading_graph.py`)

- **Purpose:** orchestrates the full agent workflow.
- **Inputs:** `selected_analysts`, `debug`, `config` dict (DEFAULT_CONFIG copy).
- **Outputs:** `propagate(company_name, trade_date, asset_type="stock")`
  returns `(final_state_dict, rating_str)` where rating ∈ Buy/Overweight/
  Hold/Underweight/Sell; `save_reports(final_state, ticker, save_path)` writes
  markdown tree.
- **Errors:** raises on graph failure; checkpoint resume transparent.
- **Example:** see repo-root `main.py`.

### B2. Vendor router (`dataflows/interface.py`)

- **Purpose:** single dispatch point from tools to configured vendors.
- **Inputs:** `route_to_vendor(method: str, *args, **kwargs)` with method ∈
  `VENDOR_METHODS`.
- **Outputs:** vendor payload (str/DataFrame) or sentinel strings:
  `NO_DATA_AVAILABLE: …` / `DATA_UNAVAILABLE: …`.
- **Errors:** raises first real vendor error when no vendor can serve and the
  category is core; optional categories degrade instead.
- **Contract:** explicit vendor chains are honored exactly — no silent
  fallback to unconfigured vendors.

### B3. Symbol normalization (`dataflows/symbol_utils.py`)

- `normalize_symbol(raw) -> str`: broker-style → Yahoo-style mapping
  (aliases → crypto rule → forex rule → passthrough). Purely syntactic.
- `crypto_base(raw) -> str | None`; `is_yahoo_safe(symbol) -> bool`.

### B4. Error taxonomy (`dataflows/errors.py`)

`VendorError` base; `NoMarketDataError(symbol, canonical, detail)`;
`VendorRateLimitError`; `VendorNotConfiguredError` (also ValueError).

### B5. Structured-output pattern (`agents/utils/structured.py`)

- `bind_structured(llm, schema, agent_name) -> llm'|None`
- `invoke_structured_or_freetext(structured_llm, plain_llm, prompt, render, agent_name) -> str`
- Contract: schema-typed result rendered to stable markdown headers; any
  structured failure falls back to one free-text attempt.

### B6. Schemas (`agents/schemas.py`)

`ResearchPlan`, `TraderProposal` (Buy/Hold/Sell + entry/stop/sizing),
`PortfolioDecision` (5-tier rating), `SentimentReport` (band, score 0–10,
confidence low/med/high), plus deterministic renderers preserving legacy
markdown headers.

### B7. Config system (`default_config.py`, `dataflows/config.py`)

`DEFAULT_CONFIG` dict; `TRADINGAGENTS_*` env overrides via `_ENV_OVERRIDES`
(type-coerced, invalid values raise); runtime access via
`get_config()/set_config()`.

### B8. Verified snapshot (`dataflows/market_data_validator.py`)

`build_verified_market_snapshot(symbol, curr_date, look_back_days=30,
indicators=None) -> str` — deterministic OHLCV+indicator ground-truth table;
look-ahead excluded; errors raise rather than fabricate.

### B9. Reporting (`reporting.py`)

`write_report_tree(final_state, ticker, save_path) -> Path` — writes
per-section markdown + `complete_report.md`.

---

## Part C — Phase 1 Interfaces (IMPLEMENTED)

All interfaces below exist in the tree with tests; signatures shown are the
implemented contracts. Source of truth is the code.

### C1. Asset registry (`tradingagents/assets/registry.py`) — IMPLEMENTED

```python
@dataclass(frozen=True)
class AssetSpec:
    asset_id: str            # canonical internal ID, e.g. "XAUUSD", "BTCUSD"
    display_name: str        # "XAU/USD"
    asset_class: AssetClass  # METAL | CRYPTO | FOREX | INDEX | EQUITY ...
    quote_currency: str      # "USD"
    provider_symbols: Mapping[str, str]   # {"yahoo": "GC=F"} / {"yahoo": "BTC-USD"}
    macro_context: tuple[str, ...]        # hints: usd_rates, fed, treasury_yields / crypto_market

get_asset(asset_id: str) -> AssetSpec          # unknown id -> AssetNotFoundError
list_assets() -> tuple[AssetSpec, ...]
```

### C2. Timeframes (`tradingagents/marketdata/timeframes.py`) — IMPLEMENTED

`Timeframe` enum: `M15`, `H1`, `H4`, `D1` with pandas-offset helpers and
serialization (`"15m"|"1h"|"4h"|"1d"`).

### C3. Normalized models (`tradingagents/marketdata/models.py`) — IMPLEMENTED

```python
class DataStatus(str, Enum): REALTIME; DELAYED; HISTORICAL; CACHED; SIMULATED

class Bar(BaseModel):     timestamp: datetime (tz-aware UTC)
                          open/high/low/close: float; volume: float | None
class OhlcvSeries(BaseModel):
    asset_id: str; timeframe: Timeframe; source: str; status: DataStatus
    bars: list[Bar]       # uniform timeframe/status enforced by validation
class Quote(BaseModel):   asset_id, timestamp (tz-aware), bid/ask/last optional,
                          source, status
```

### C4. Provider interface (`tradingagents/marketdata/provider.py`) — IMPLEMENTED

```python
class MarketDataProvider(Protocol):
    name: str
    def get_ohlcv(self, asset: AssetSpec, timeframe: Timeframe, *,
                  limit: int | None = None,
                  start: datetime | None = None,
                  end: datetime | None = None) -> OhlcvSeries: ...
    def get_quote(self, asset: AssetSpec) -> Quote | None: ...
# First impl: YahooMarketDataProvider wrapping existing dataflows/y_finance code.
```

Failure behavior: providers raise the existing `VendorError` taxonomy; the
engine converts them to per-section unavailable statuses. Caching stays at the
existing OHLCV cache layer (status becomes CACHED when served from disk).

### C5. Indicator engine (`analysis/indicators.py`) — IMPLEMENTED

`compute_indicators(series: OhlcvSeries) -> TechnicalSnapshot` — RSI(14),
MACD(12,26,9), SMA20/50/200, EMA10, Bollinger(20,2), ATR(14), momentum(n),
realized volatility; all values carry lookback-sufficiency flags; missing =
explicit `None` + reason, never silent NaN.

### C6. Research schemas (`research/schemas.py`) — IMPLEMENTED

`TechnicalAnalysis`, `MacroAnalysis`, `NewsAnalysis` (items with source +
published_at + relevance + sentiment), `SentimentAnalysis`,
`BullCase`, `BearCase`, `ResearchManagerVerdict`, composing into
`ResearchReport` and finally:

```python
class ResearchSignal(BaseModel):
    asset_id: str; generated_at: datetime; timeframe: Timeframe
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float                 # [0,1], heuristic-documented
    entry_reference / stop_loss_reference / take_profit_reference: float | None
    risk_level: Literal["LOW","MEDIUM","HIGH"]
    thesis: str; supporting_factors: list[str]; opposing_factors: list[str]
    invalidation_conditions: list[str]
    data_sources: list[DataSourceRef]; models_used: list[str]
    disclaimer: Literal["RESEARCH SIGNAL — NOT EXECUTED"]  # always set
```

### C7. Research engine (`research/engine.py`) — IMPLEMENTED

`ResearchEngine(config).run(asset_id: str, timeframe: Timeframe) ->
ResearchResult(report, signal)` — orchestrates providers → indicators →
research graph → deterministic signal assembly; emits structured log events;
degrades per-section on failure; refuses to emit a signal without verified
market data.

---

*Update this file whenever an external or stable internal API changes.*
