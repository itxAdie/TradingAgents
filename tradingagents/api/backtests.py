"""Background backtest execution for the dashboard.

Wraps the unchanged Phase 2 CLI wiring (cli/backtest.py) in a single-worker
registry: one concurrent run, job status persisted as JSON next to the
``BacktestReport`` artifact, lifecycle events published on the bus, every
submission audited. No backtesting logic lives here — only orchestration
(ARCHITECTURE.md P4.3).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tradingagents.api.audit import AuditLog
from tradingagents.api.eventbus import EventBus


class BacktestConfigError(ValueError):
    """Invalid backtest request parameters."""


class BacktestConflictError(RuntimeError):
    """Another backtest is already running (single-worker registry)."""


class BacktestStartRequest(BaseModel):
    asset_id: str
    timeframe: str = "1h"
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_capital: float = Field(default=10_000.0, gt=0)
    slippage_bps: float = Field(default=1.0, ge=0)
    spread_bps: float = Field(default=2.0, ge=0)
    commission_bps: float = Field(default=0.5, ge=0)
    sizing_mode: str = Field(default="pct_equity", pattern="^(fixed_notional|fixed_quantity|pct_equity)$")
    sizing_value: float = Field(default=0.95, gt=0)
    warmup_bars: int = Field(default=210, ge=10)
    include_ai: bool = False  # LLM research steps COST MONEY — explicit opt-in
    include_walk_forward: bool = False
    fetch: bool = False  # download missing Yahoo data first (network)


class BacktestJob(BaseModel):
    """Persisted job record ({cache}/backtests/{run_id}.job.json)."""

    run_id: str
    status: str = "queued"  # queued | running | completed | failed
    params: BacktestStartRequest
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    report_path: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class BacktestRunRegistry:
    def __init__(
        self,
        root: Path,
        *,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
    ):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._bus = bus
        self._audit = audit
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._dataset_meta: Any = None

    # -- paths -----------------------------------------------------------------

    def _job_path(self, run_id: str) -> Path:
        return self._root / f"{run_id}.job.json"

    def _report_path(self, run_id: str) -> Path:
        return self._root / f"backtest_report_{run_id}.json"

    # -- public surface ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, request: BacktestStartRequest) -> BacktestJob:
        self._validate(request)
        with self._lock:
            if self.running:
                raise BacktestConflictError(
                    "another backtest is already running; wait for it to finish"
                )
            job = BacktestJob(
                run_id=f"bt-{_now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
                params=request,
                submitted_at=_now(),
            )
            self._save_job(job)
            thread = threading.Thread(
                target=self._execute, args=(job,), name="backtest-worker", daemon=True
            )
            self._thread = thread
        if self._audit is not None:
            self._audit.record("backtest_submitted", run_id=job.run_id, **request.model_dump())
        if self._bus is not None:
            self._bus.notify(
                "backtest_submitted",
                {"run_id": job.run_id, "asset_id": request.asset_id,
                 "timeframe": request.timeframe},
            )
        thread.start()
        return job

    def get(self, run_id: str) -> BacktestJob | None:
        path = self._job_path(run_id)
        if not path.exists():
            return None
        try:
            return BacktestJob.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_jobs(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[BacktestJob], int]:
        jobs: list[BacktestJob] = []
        for path in self._root.glob("*.job.json"):
            try:
                jobs.append(
                    BacktestJob.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except Exception:
                continue
        jobs.sort(key=lambda j: j.submitted_at, reverse=True)
        total = len(jobs)
        return jobs[offset : offset + limit], total

    # -- internals -----------------------------------------------------------------

    def _validate(self, request: BacktestStartRequest) -> None:
        from tradingagents.assets.registry import UnknownAssetError, get_asset
        from tradingagents.marketdata.timeframes import Timeframe

        try:
            get_asset(request.asset_id.strip().upper())
        except (UnknownAssetError, KeyError) as exc:
            raise BacktestConfigError(f"unknown asset id: {request.asset_id!r}") from exc
        try:
            tf = Timeframe(request.timeframe.strip().lower())
        except ValueError as exc:
            raise BacktestConfigError(
                f"unsupported timeframe {request.timeframe!r}"
            ) from exc
        start_dt, end_dt = _parse_date(request.start), _parse_date(request.end)
        if start_dt >= end_dt:
            raise BacktestConfigError("start must be before end")
        max_days = tf.max_history_days() * 3  # sanity ceiling, not a hard vendor rule
        if (end_dt - start_dt).days > max_days:
            raise BacktestConfigError(
                f"period {(end_dt - start_dt).days} days exceeds "
                f"{max_days} for {tf.value}"
            )

    def _save_job(self, job: BacktestJob) -> None:
        tmp = self._job_path(job.run_id).with_suffix(".tmp")
        tmp.write_text(job.model_dump_json(indent=1), encoding="utf-8")
        tmp.replace(self._job_path(job.run_id))

    def _update(self, job: BacktestJob, **updates: Any) -> None:
        job = job.model_copy(update=updates)
        self._save_job(job)

    def _emit(self, event: str, job: BacktestJob, **extra: Any) -> None:
        if self._bus is not None:
            self._bus.notify(
                event,
                {"run_id": job.run_id, "asset_id": job.params.asset_id,
                 "timeframe": job.params.timeframe, **extra},
            )

    def _execute(self, job: BacktestJob) -> None:
        self._update(job, status="running", started_at=_now())
        self._emit("backtest_started", job)
        try:
            report_path = self._run_backtest(job)
            self._update(
                job, status="completed", finished_at=_now(), report_path=str(report_path)
            )
            self._emit("backtest_completed", job, report_path=str(report_path))
        except Exception as exc:
            self._update(job, status="failed", finished_at=_now(), error=str(exc)[:1000])
            self._emit("backtest_failed", job, error=str(exc)[:500])
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _run_backtest(self, job: BacktestJob) -> Path:
        # Lazy imports keep API startup light and make test monkeypatching easy.
        from tradingagents.backtest.baselines import (
            BuyAndHoldStrategy,
            MomentumStrategy,
            SmaCrossStrategy,
        )
        from tradingagents.backtest.config import ExecutionConfig, RiskLimits, SizingPolicy
        from tradingagents.backtest.engine import (
            build_report,
            run_backtest,
            run_walk_forward,
        )
        from tradingagents.backtest.historical.store import JsonDataStore
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.marketdata.timeframes import Timeframe

        params = job.params
        cache_dir = Path(str(DEFAULT_CONFIG.get("data_cache_dir", ".cache")))
        store = JsonDataStore(cache_dir / "historical")

        from tradingagents.assets.registry import get_asset

        spec = get_asset(params.asset_id.strip().upper())
        tf = Timeframe(params.timeframe.strip().lower())
        start_dt, end_dt = _parse_date(params.start), _parse_date(params.end)

        dataset = self._load_dataset(store, spec.asset_id, tf, start_dt, end_dt, params.fetch)

        strategies: list[object] = [
            BuyAndHoldStrategy(),
            SmaCrossStrategy(),
            MomentumStrategy(),
        ]
        ai_strategy = None
        enabled_components = ["market_data"]
        disabled_components = ["news", "sentiment", "macro", "ai_research"]
        if params.include_ai:
            from tradingagents.backtest.engine import AIResearchStrategy
            from tradingagents.backtest.research_cache import ResearchCache

            ai_strategy = AIResearchStrategy(
                asset_id=spec.asset_id,
                timeframe=tf,
                research_config=dict(DEFAULT_CONFIG),
                enable_macro=False,
                research_cache=ResearchCache(cache_dir / "research_cache"),
            )
            strategies.append(ai_strategy)
            enabled_components = ["market_data", "ai_research"]
            disabled_components = ["news", "sentiment", "macro"]

        exec_cfg = ExecutionConfig(
            slippage_bps=params.slippage_bps,
            spread_bps=params.spread_bps,
            commission_per_side_bps=params.commission_bps,
        )
        sizing = SizingPolicy(mode=params.sizing_mode, value=params.sizing_value)
        limits = RiskLimits()

        output = run_backtest(
            dataset=dataset,
            strategies=strategies,
            initial_capital=params.initial_capital,
            execution_cfg=exec_cfg,
            sizing=sizing,
            limits=limits,
            warmup_bars=min(params.warmup_bars, max(10, len(dataset.bars) - 5)),
        )
        report = build_report(
            dataset=dataset,
            dataset_meta=self._dataset_meta,
            results=output.results,
            initial_capital=params.initial_capital,
            execution_cfg=exec_cfg,
            sizing=sizing,
            limits=limits,
            enabled_components=enabled_components,
            disabled_components=disabled_components,
            ai_strategy=ai_strategy,
            run_id=job.run_id,
        )
        if params.include_walk_forward:
            aggregated = run_walk_forward(
                dataset=dataset,
                strategies=strategies,
                wf_config=self._wf_config(),
                initial_capital=params.initial_capital,
                execution_cfg=exec_cfg,
                sizing=sizing,
                limits=limits,
            )
            report.walk_forward = [
                {
                    "strategy_id": sid,
                    "windows": [w.model_dump(mode="json") for w in windows],
                    "aggregate": agg.model_dump(mode="json"),
                }
                for sid, (windows, agg) in aggregated.items()
            ]
        path = report.to_json(self._report_path(job.run_id))
        for strategy_id, ledger in output.ledgers.items():
            ledger.to_json(self._root / f"trades_{strategy_id}_{job.run_id}.json")
        return path

    def _load_dataset(self, store: Any, asset_id: str, tf: Any, start_dt: Any,
                      end_dt: Any, fetch: bool) -> Any:
        try:
            dataset, meta = store.load(asset_id, tf)
            covered = (
                dataset.bars[0].timestamp <= start_dt
                and dataset.bars[-1].timestamp >= end_dt
            )
            if covered:
                self._dataset_meta = meta
                return dataset
            if not fetch:
                raise BacktestConfigError(
                    f"stored window {dataset.bars[0]:%Y-%m-%d}.."
                    f"{dataset.bars[-1]:%Y-%m-%d} does not cover "
                    f"{start_dt:%Y-%m-%d}..{end_dt:%Y-%m-%d}; resubmit with fetch=true"
                )
        except FileNotFoundError:
            if not fetch:
                raise BacktestConfigError(
                    f"no stored dataset for {asset_id} @ {tf.value}; "
                    "resubmit with fetch=true to download from Yahoo"
                ) from None
        dataset, meta = _fetch(store, asset_id, tf, start_dt, end_dt)
        self._dataset_meta = meta
        return dataset

    def _wf_config(self) -> Any:
        from tradingagents.backtest.walkforward import WalkForwardConfig

        return WalkForwardConfig()


def _fetch(store: Any, asset_id: str, tf: Any, start_dt: Any, end_dt: Any) -> tuple[Any, Any]:
    from tradingagents.assets.registry import get_asset
    from tradingagents.backtest.historical.yahoo_history import fetch_and_store

    return fetch_and_store(get_asset(asset_id), tf, start=start_dt, end=end_dt, store=store)


__all__ = [
    "BacktestConflictError",
    "BacktestConfigError",
    "BacktestJob",
    "BacktestRunRegistry",
    "BacktestStartRequest",
]
