# ACCESS_INFO.md

Access and configuration reference. **This file never contains real
secrets** — only variable names, placeholders, and links. Copy `.env.example`
to `.env` and fill in the values you need.

---

## 1. Local Setup Requirements

- Python **3.10–3.13** (repo targets `>=3.10`; CI tests 3.10/3.11/3.12/3.13)
- Install: `pip install -e ".[dev]"` (adds pytest + ruff)
- Optional extra for AWS Bedrock: `pip install "tradingagents[bedrock]"`
- Docker alternative: `cp .env.example .env` then `docker compose run --rm tradingagents`
- Run tests offline: `pytest -q` (no keys required — placeholders are injected)

---

## 2. Environment Variables

### 2.1 LLM provider keys (set at least ONE, matching your chosen provider)

| Variable | Provider | Required for | Obtain |
|---|---|---|---|
| `OPENAI_API_KEY=` | OpenAI (GPT) | provider `openai` | platform.openai.com |
| `GOOGLE_API_KEY=` | Google Gemini | provider `google` | aistudio.google.com/apikey |
| `ANTHROPIC_API_KEY=` | Anthropic Claude | provider `anthropic` | console.anthropic.com |
| `XAI_API_KEY=` | xAI Grok | provider `xai` | console.x.ai |
| `DEEPSEEK_API_KEY=` | DeepSeek | provider `deepseek` | platform.deepseek.com |
| `DASHSCOPE_API_KEY=` | Qwen international | provider `qwen` (intl endpoint) | dashscope-intl.aliyuncs.com |
| `DASHSCOPE_CN_API_KEY=` | Qwen China | provider qwen CN endpoint | dashscope.aliyuncs.com |
| `ZHIPU_API_KEY=` | GLM via Z.AI intl | zhipu intl | z.ai |
| `ZHIPU_CN_API_KEY=` | GLM via BigModel China | bigmodel.cn |
| `MINIMAX_API_KEY=` | MiniMax global | api.minimax.io |
| `MINIMAX_CN_API_KEY=` | MiniMax China | api.minimaxi.com |
| `OPENROUTER_API_KEY=` | OpenRouter | openrouter.ai |
| `MISTRAL_API_KEY=` | Mistral | console.mistral.ai |
| `MOONSHOT_API_KEY=` | Moonshot Kimi | platform.moonshot.ai |
| `GROQ_API_KEY=` | Groq | console.groq.com |
| `NVIDIA_API_KEY=` | NVIDIA NIM | build.nvidia.com |

Placeholders: use e.g. `OPENAI_API_KEY=YOUR_API_KEY_HERE`.

### 2.2 Enterprise / local / cloud LLM endpoints

| Variable | Purpose | Default |
|---|---|---|
| `AZURE_OPENAI_API_KEY=`, plus `.env.enterprise` settings | Azure OpenAI (copy `.env.enterprise.example`) | — |
| `AWS_BEARER_TOKEN_BEDROCK=` | Bedrock API-key auth (bearer) — OR standard AWS chain below | unset |
| `AWS_ACCESS_KEY_ID=` / `AWS_SECRET_ACCESS_KEY=` / `AWS_PROFILE=` | AWS credential chain for Bedrock | env/profile/IAM role |
| `AWS_DEFAULT_REGION=` | Bedrock region (required either way), e.g. `us-west-2` | unset |
| `OLLAMA_BASE_URL=` | Remote Ollama; unset = `http://localhost:11434/v1` | local |
| `OPENAI_COMPATIBLE_API_KEY=` | Key for custom OpenAI-compatible server (vLLM/LM Studio/llama.cpp); not needed for local servers | unset |
| `TRADINGAGENTS_LLM_BACKEND_URL=` | Base URL for `openai_compatible` provider | provider default |

### 2.3 Market/macro/news data keys

| Variable | Required for | Without it |
|---|---|---|
| `ALPHA_VANTAGE_API_KEY=` (optional) | Alpha Vantage as vendor (`data_vendors` config) | AV vendor raises `VendorNotConfiguredError`; yfinance default unaffected |
| `FRED_API_KEY=` (optional) | Macro analyst context via FRED (rates, inflation, labor, growth) | macro section reports unavailable; run continues |

Free FRED key: fred.stlouisfed.org/docs/api/api_key.html.
Alpha Vantage free key: alphavantage.co/support/#api-key.

Yahoo Finance, StockTwits, Reddit, Polymarket need **no credentials**.

### 2.4 Behavior overrides (all optional)

| Variable | Effect |
|---|---|
| `TRADINGAGENTS_LLM_PROVIDER` | skip interactive provider pick (openai, google, anthropic, deepseek, ollama, openai_compatible, …) |
| `TRADINGAGENTS_DEEP_THINK_LLM` / `TRADINGAGENTS_QUICK_THINK_LLM` | model IDs per tier |
| `TRADINGAGENTS_OUTPUT_LANGUAGE` | report language (default English) |
| `TRADINGAGENTS_MAX_DEBATE_ROUNDS` / `TRADINGAGENTS_MAX_RISK_ROUNDS` | debate depth (default 1) |
| `TRADINGAGENTS_CHECKPOINT_ENABLED` | SQLite checkpoint resume |
| `TRADINGAGENTS_TEMPERATURE` | sampling temperature when supported |
| `TRADINGAGENTS_LLM_MAX_RETRIES` | SDK retry budget across providers |
| `TRADINGAGENTS_OPENAI_REASONING_EFFORT` / `TRADINGAGENTS_GOOGLE_THINKING_LEVEL` / `TRADINGAGENTS_ANTHROPIC_EFFORT` | reasoning/thinking depth knobs |
| `TRADINGAGENTS_RESULTS_DIR` | output root (default `~/.tradingagents/logs`) |
| `TRADINGAGENTS_CACHE_DIR` | data cache root (default `~/.tradingagents/cache`) |
| `TRADINGAGENTS_MEMORY_LOG_PATH` | decision log file (default under `~/.tradingagents/memory/`) |
| `TRADINGAGENTS_BENCHMARK_TICKER` | override alpha benchmark for reflections |

Invalid values fail loudly at startup (type-coerced against defaults).

---

## 3. Which Credentials Are Needed For Which Feature

| Feature | Minimum credentials |
|---|---|
| Any analysis run | One LLM key from §2.1 (or local Ollama/OpenAI-compatible server → none) |
| XAUUSD research (Phase 1 target) | LLM only — market data via Yahoo (`GC=F`), no key |
| BTCUSD research (Phase 1 target) | LLM only — market data via Yahoo (`BTC-USD`), no key |
| Macro context (Fed/rates/inflation) for gold | + `FRED_API_KEY` |
| Alpha Vantage as alternate vendor | + `ALPHA_VANTAGE_API_KEY`, set `data_vendors` accordingly |
| News & sentiment | No keys (yfinance news + StockTwits + Reddit) |
| Backtesting (Phase 2) | LLM key only when replaying AI strategies; deterministic baselines need none |
| Paper trading (Phase 3) | Same as research; **no broker credentials exist or are read anywhere** — execution is simulated arithmetic, state is local JSON |

---

## 4. Security Notes

- Never commit `.env` or any real key; `.gitignore` already excludes it.
- Use placeholders (`YOUR_API_KEY_HERE`) in examples and docs.
- Rotate any key that ever lands in a log, screenshot, or commit history.
- Broker/account credentials (API keys, account numbers, OAuth tokens) are
  **not used, stored, or requested** by any phase implemented so far; Phase 5
  (broker integration) is out of scope and nothing in the codebase imports a
  broker SDK (enforced by `tests/test_paper_safety.py`).
