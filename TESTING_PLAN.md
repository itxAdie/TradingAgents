# TESTING_PLAN.md

Testing strategy for the research platform. Baseline (2026-08-23, pre-Phase 1):
**576 passed, 2 skipped, 69 subtests** in ~2 min; `ruff check .` clean.
After Phase 1 implementation (2026-08-23): **658 passed, 2 skipped,
69 subtests** (~3 min); `ruff check .` clean — 82 new tests, zero regressions.
After Phase 2 implementation (2026-08-24): **739 passed, 2 skipped,
69 subtests** (~3 min); `ruff check .` clean — 81 new backtest tests, zero
regressions.

## Principles

1. **Offline by default.** Tests must not require paid APIs or network
   access. All external providers are mocked/faked. Live-API tests must be
   explicitly marked (`@pytest.mark.integration`) and skip cleanly without
   credentials (existing precedent: `tests/test_deepseek_reasoning.py`).
2. **Reuse existing harness.** `tests/conftest.py` already injects
   placeholder API keys, resets the global dataflows config per test, and
   provides `mock_llm_client`. New tests build on it.
3. **Markers:** `unit` (fast/isolated), `integration`
   (optional/live/external), `smoke` (sanity) — already registered in
   `pyproject.toml`.
4. Every Phase 1 feature ships with unit + failure-path tests before being
   considered complete. Existing tests must keep passing (regression gate).

---

## 1. Unit Tests (new)

Implemented files below; "planned" names that differed in the final tree are
noted. All are offline and marked `unit` unless stated otherwise.

| Target | File (implemented) | Must verify | Status |
|---|---|---|---|
| Asset registry | `tests/test_asset_registry.py` | canonical IDs resolve (`XAUUSD`, `BTCUSD`); provider symbol mapping; unknown asset raises clear error; registry is the single lookup point for engine/agents | done |
| Timeframes | `tests/test_timeframes.py` | members 15m/1h/4h/1d; bar-interval math; invalid timeframe rejected; serialization round-trip; H4 resampling flag; staleness windows | done |
| Market-data models | `tests/test_marketdata_models.py` | `OhlcvSeries`/`Quote` validation; tz-aware timestamps required; mixed-timeframe rows rejected; `DataStatus` propagation via `classify_status`; no silent status mixing | done |
| Yahoo provider adapter | `tests/test_yahoo_provider.py` | wraps existing yfinance functions; asset to Yahoo-symbol mapping; correct status label (delayed for GC=F); limit/window handling — all via mocked yfinance | done |
| Provider interface | covered inside `test_research_engine.py` (`FakeProvider` satisfies `MarketDataProvider` and plugs into the engine unchanged) + protocol import checks | done |
| Indicator engine | `tests/test_indicators_engine.py` | RSI/MACD/SMA/EMA/Bollinger/ATR/momentum/volatility computed on synthetic series with explicit pandas expressions (see ARCHITECTURE deviation note re stockstats); insufficient data yields explicit missing markers (never silent NaN); deterministic across runs; crypto annualization factor | done |
| Signal schemas | `tests/test_research_schemas.py` | `ResearchReport`/`ResearchSignal` validation; only BUY/SELL/HOLD allowed; confidence bounds; NOT-EXECUTED label present in every rendering | done |
| Confidence aggregation | `tests/test_confidence.py` | documented weights 0.40/0.25/0.20/0.15; agreement / data-completeness / consistency inputs move score as specified; bounded [0,1]; labelled experimental heuristic | done |
| Signal assembly | `tests/test_signal_assembly.py` | entry/stop/target derived deterministically from verified bars + ATR (1.5×ATR stop, 2R target); stale data blocks signal emission; HOLD carries no protective levels | done |
| Structured parsing | folded into `tests/test_research_engine.py` (`FakeStructuredLLM` queue: malformed/missing outputs mark sections unavailable; no free-text fallback) | done |
| Research logging | `tests/test_research_logging.py` | start/end events carry asset/timeframe/model/data timestamps; failures recorded; secrets filtered; datetimes ISO-formatted; unserializable values stringified | done |

Existing suites already cover: symbol normalization paths, vendor routing and
error taxonomy, OHLCV cache freshness/staleness guards, date boundaries and
look-ahead filtering, structured agent prompts, checkpoint resume, CLI config
precedence, LLM provider knobs, news windows, i18n.

## 1b. Phase 2 Unit Tests (backtesting engine)

All offline (synthetic OHLC series + fake providers); 81 tests across eight
files. Key invariants each file pins down:

| File | Verifies | Tests |
|---|---|---|
| `tests/test_backtest_clock.py` | tz handling, UTC normalization, backward-set rejection, monotone setting, clock never self-advances | 6 |
| `tests/test_backtest_historical.py` | validation gate hard-rejects bad rows (non-positive/high<max(open,close)/unordered/duplicate), gaps flagged not fixed; JSON store roundtrip, corruption detection via content hash, retention guard, relabeling to HISTORICAL | 15 |
| `tests/test_backtest_replay_provider.py` | future-bar invisibility (`timestamp <= clock.now()`), visibility exactly at bar close, NoMarketDataError before first bar/wrong asset, wrong-timeframe and non-HISTORICAL rejection, slicing clamped to cutoff, call telemetry | 10 |
| `tests/test_backtest_execution_portfolio.py` | HOLD never schedules; next-bar-open fills with adverse cost math; pending-fill replacement; flip = exit+entry at same open; stop-wins pessimism; TP fires; end_of_data honest settlement; exposure/cash gates; settled-cash portfolio accounting hand-computed; sizing modes; ledger CSV/JSON export | 14 |
| `tests/test_backtest_analytics.py` | hand-computed return/drawdown/trade stats/profit factor; N/A-with-reason for no-losses/short windows/zero dispersion; B&H benchmark formula first-decision-open → last-close | 7 |
| `tests/test_backtest_baselines_walkforward.py` | exact SMA cross indices on synthetic closes; momentum sign rule; B&H once-only; ATR level math; window frame arithmetic; aggregation median/best/worst/% profitable; empty-input consistency notes | 12 |
| `tests/test_backtest_engine.py` | slice_upto boundary; output shape; **determinism** across repeated runs; **prefix isolation**: decisions identical when future bars are truncated away at multiple cut points (anti look-ahead proof); provenance timestamps; walk-forward e2e grouping; report schema/disclaimer/config-hash stability | 10 |
| `tests/test_backtest_ai_strategy.py` | AIResearchStrategy binds sim-clock `now_fn`, disables news/sentiment(/macro), default LLM client construction, cache miss→hit→growing-history-miss, cross-instance persistence, corrupt-entry treated as miss, sha256 key form | 8 |

## 2. Integration Tests (new, still offline)

Implemented as engine-level tests in `tests/test_research_engine.py` (full
graph on fakes; node ordering via call-recording fakes; end-to-end offline run
yields valid `ResearchSignal` with NOT-EXECUTED label). The four originally
planned files were consolidated into one engine suite; coverage is equivalent.

| Flow | File (implemented) | Approach | Status |
|---|---|---|---|
| Provider -> analysis -> agents -> signal | `tests/test_research_engine.py` (`TestHappyPath`) | fake provider + queued fake LLM; assert full report tree + BUY signal | done |
| Failure isolation | `tests/test_research_engine.py` (`TestFailurePaths`) | every agent failing still yields deterministic fallback signal; per-agent failures recorded; stale data emits no signal | done |
| Freshness gate | `tests/test_research_engine.py` (`TestFreshnessGate`) | stale `OhlcvSeries` aborts before LLM calls | done |

## 3. Mock Strategy

- External market/news/sentiment APIs: monkeypatch at vendor-function seams
  (`dataflows.interface.route_to_vendor`, `dataflows.y_finance`,
  `yfinance_news`, `stocktwits`, `reddit`) — same boundaries existing tests use.
- LLMs: fake chat models returning queued/canned structured outputs
  (precedent: `mock_llm_client`, structured-agent tests). CI never calls real
  providers.
- Optional live checks stay behind the `integration` marker plus an env-key
  guard so they skip cleanly locally.

## 4. Failure-Path Tests (mandatory)

Status after Phase 1: F1/F4 covered by `TestFreshnessGate` +
`NoMarketDataError` engine tests; F2/F3 via provider empty-frame →
`NoMarketDataError` tests; F5/F6 via `TestFailurePaths` and
`FakeStructuredLLM` malformed-output cases; F7 is inherited from the existing
`yf_retry` suite (unchanged); F8 inherited from existing env-var startup
checks (unchanged).

| # | Failure injected | Expected behavior |
|---|---|---|
| F1 | Market-data API timeout | typed error caught; data marked unavailable; **no signal emitted** from stale/unknown data |
| F2 | Source unavailable across all vendors | NO_DATA sentinel path; report shows unavailable status |
| F3 | Malformed vendor response | parse failure logged and treated as unavailable; never silently empty |
| F4 | Invalid symbol / missing market data | `NoMarketDataError` detail surfaces; explicit gap in report or loud pre-LLM abort |
| F5 | One LLM agent fails mid-graph | failure recorded with reason; remaining sections continue; report lists missing analysis |
| F6 | Malformed LLM output (bad JSON/schema) | structured retry falls back once, then section marked unavailable; nothing fabricated |
| F7 | Vendor rate limiting | backoff then next configured vendor (existing chain semantics) |
| F8 | Missing environment variables | fail loudly at startup with actionable message |

## 5. Regression Testing

- Full existing suite must pass on every change: `pytest -q`.
- Lint gate: `ruff check .` (strict select already configured).
- CI parity: GitHub Actions runs pytest on Python 3.10–3.13 plus ruff; local
  baseline commands above mirror it.
- Any intentional behavior change requires updating the affected existing test
  and documenting why in `SESSION_LOG.md`.

## 6. Commands

```bash
.venv/bin/pytest -q          # full suite (offline)
.venv/bin/ruff check .       # lint
```
