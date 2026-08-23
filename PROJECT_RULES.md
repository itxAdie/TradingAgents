# PROJECT_RULES.md

Permanent engineering rules for this repository. Every contribution (human or
AI agent) must respect them. Rules are ordered by theme; the *Trading safety*
section is non-negotiable.

---

## 1. Architecture

1. **Extend, don't duplicate.** Prefer extending existing abstractions
   (`dataflows` vendor layer, `agents` factories, `graph` setup,
   `schemas.py`, `structured.py`) over creating parallel systems.
2. **No unnecessary frameworks.** Do not add a framework when the existing
   stack (LangGraph, Pydantic, stockstats, requests/yfinance) solves it.
   New dependencies require documented justification in `SESSION_LOG.md`.
3. **Layer separation.** Keep these layers strictly separated:
   - data providers (`tradingagents/dataflows`, new `marketdata`),
   - deterministic analysis logic (`analysis`),
   - LLM agents (`tradingagents/agents`, `research`),
   - orchestration/workflow (`tradingagents/graph`, research engine),
   - signal schemas (`research/schemas`),
   - presentation (CLI, reporting).
   Lower layers must never import higher ones.
4. **Decouple from future phases.** Phase 1 code must not import or reference
   UI, dashboards, brokers, or execution. Future phases consume Phase 1
   interfaces; never the reverse.
5. **Design for reuse.** New components must be usable by later phases
   (backtesting, paper trading, API) without modification.
6. **Centralize domain concepts.** Assets, timeframes, and vendor symbols live
   in registries/enums — never scattered as raw strings through agents.

## 2. Code Quality

7. Follow existing conventions: ruff config in `pyproject.toml`
   (line-length 100, E501 exempt), type hints on public functions,
   docstrings that explain *why* with issue references where relevant.
8. Functions/classes do one thing; keep agent factories small by reusing
   shared helpers (`structured.py`, `agent_utils.py`).
9. No magic numbers or hardcoded configuration — use `DEFAULT_CONFIG`,
   module-level named constants, or registry entries.
10. Handle failures explicitly. Never silently swallow exceptions:
    catch narrowly, log with context, and degrade to an explicit sentinel
    status (e.g. `news_status = "unavailable"`) rather than fabricating data.
11. Deterministic computations and AI-generated analysis must be clearly
    distinguishable in code structure and in output artifacts.

## 3. Configuration & Secrets

12. **Secrets never enter Git.** No API keys, tokens, passwords, or private
    URLs in code, tests, docs, or logs.
13. All provider credentials come from environment variables (see
    `.env.example`). Never hardcode keys; never log secrets.
14. Whenever a new environment variable is introduced: update `.env.example`
    **and** `ACCESS_INFO.md` in the same change.
15. Config changes flow through `default_config.DEFAULT_CONFIG` /
    `_ENV_OVERRIDES` (type-coerced, fail-loudly) — not ad-hoc `os.getenv`
    sprinkled through modules.

## 4. Data Integrity

16. Label every datum with its nature: **real-time**, **delayed**,
    **historical**, **cached**, or **simulated/test**. Never present stale or
    simulated data as real-time.
17. Preserve timezone information on timestamps; do not silently mix bars of
    different timeframes or different data statuses within one series.
18. Missing data stays missing: report unavailability explicitly; never
    interpolate, estimate, or let an LLM invent values for gaps.

## 5. AI / LLM Usage

19. AI output must be structured (Pydantic schemas via the existing
    `with_structured_output` pattern) wherever it feeds another component;
    prose is for humans only.
20. An LLM response is never guaranteed financial truth. Exact numeric claims
    must be grounded in deterministic snapshots (see
    `get_verified_market_snapshot` pattern); conflicting numbers get flagged,
    not reconciled by invention.
21. LLM failures must not crash the pipeline: record failure + reason, mark
    the section unavailable, continue where possible.
22. Agents must not hallucinate market conditions: only analyze data actually
    fetched and shown to them; cite source + timestamp for news/sentiment.

## 6. Trading Safety (NON-NEGOTIABLE)

23. This project is **RESEARCH ONLY**. No live trading, no broker order
    execution, no automatic real-money trading, no order-placement code paths
    — even if a dependency makes it technically possible.
24. All outputs are labelled `RESEARCH SIGNAL — NOT EXECUTED`. No claims of
    profitability, certainty, or suitability.

## 7. Backward Compatibility

25. Avoid breaking existing TradingAgents functionality without a documented,
    justified reason recorded in `SESSION_LOG.md`.
26. If a public API must change: document the change in
    `API_DOCUMENTATION.md`, preserve compatibility shims where practical,
    update tests, update docs.

## 8. Testing & Documentation Discipline

27. No major feature lands without tests following `TESTING_PLAN.md`
    (unit + failure-path at minimum; external APIs mocked, never called live).
28. Keep documentation truthful: update `ARCHITECTURE.md`,
    `API_DOCUMENTATION.md`, `ACCESS_INFO.md`, `TESTING_PLAN.md`, and append
    to `SESSION_LOG.md` whenever the corresponding reality changes.
29. `SESSION_LOG.md` entries are append-only; never rewrite history there.

## 9. Git Safety

30. Inspect `git status` before modifying files; never destroy uncommitted
    user work; no force-reset/checkout without explicit permission.
31. Don't touch `.gitignore` unnecessarily; keep commits focused and
    reviewable; never commit secrets or generated artifacts.

## 10. Keeping GitHub Up To Date

32. **Push completed work to GitHub.** A unit of work is only "done" once its
    tests pass (`pytest -q`), lint is clean (`ruff check .`), and the change
    is committed **and pushed** to the project's GitHub remote. Do not leave
    finished, verified work sitting only on a local machine.
33. Commit in small, logical units with descriptive messages (what + why);
    push after each verified step rather than accumulating one giant commit.
34. Never force-push to shared branches, never push directly to protected
    branches without permission, and re-verify `git status`/`git diff` before
    every push so no secrets, credentials, or generated artifacts leak to
    the remote.
