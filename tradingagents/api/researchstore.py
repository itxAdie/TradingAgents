"""Research-artifact persistence + a report-persisting research runner.

Phase 1/3 engines return ``AssembledResult`` in memory only; the CLI saved
reports opt-in. The dashboard needs every live research run queryable, so
:class:`PersistingResearchRunner` wraps the production
``LiveResearchRunner`` (RunnerProtocol seam — zero engine changes) and
persists report + signal under::

    {cache_dir}/research_runs/index.jsonl     one summary row per run
    {cache_dir}/research_runs/{run_id}/report.json
    {cache_dir}/research_runs/{run_id}/signal.json   (when a signal assembled)

Listing/filter/pagination is served from the index file.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tradingagents.api.eventbus import EventBus
from tradingagents.research.assembly import AssembledResult


class ResearchRunSummary(BaseModel):
    """Index row: everything list views need without loading full reports."""

    run_id: str
    asset_id: str
    timeframe: str
    generated_at: datetime
    signal_action: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    no_signal_reason: str = ""
    models_used: list[str] = Field(default_factory=list)
    market_data_timestamp: datetime | None = None


class ResearchArtifactStore:
    """JSONL-indexed store of research runs; newest first on read."""

    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def index_path(self) -> Path:
        return self._root / "index.jsonl"

    def save_result(
        self,
        result: AssembledResult,
        *,
        model_ids: list[str] | None = None,
    ) -> ResearchRunSummary:
        report = result.report
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_id = f"rs-{stamp}-{uuid.uuid4().hex[:6]}"
        run_dir = self._root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(
            report.model_dump_json(indent=1), encoding="utf-8"
        )
        if result.signal is not None:
            (run_dir / "signal.json").write_text(
                result.signal.model_dump_json(indent=1), encoding="utf-8"
            )
        summary = ResearchRunSummary(
            run_id=run_id,
            asset_id=report.asset_id,
            timeframe=report.timeframe,
            generated_at=report.generated_at,
            signal_action=(
                result.signal.action.value if result.signal is not None else None
            ),
            confidence=result.signal.confidence if result.signal else None,
            no_signal_reason=result.no_signal_reason,
            models_used=model_ids or [],
            market_data_timestamp=report.market_data_timestamp,
        )
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(summary.model_dump_json() + "\n")
        os.chmod(self.index_path, 0o600)
        return summary

    # -- reads -----------------------------------------------------------------

    def _read_index(self) -> list[ResearchRunSummary]:
        if not self.index_path.exists():
            return []
        rows: list[ResearchRunSummary] = []
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(ResearchRunSummary.model_validate_json(line))
            except Exception:
                continue  # a torn final line must not kill the listing
        rows.sort(key=lambda r: r.generated_at, reverse=True)
        return rows

    def list_runs(
        self,
        *,
        asset_id: str | None = None,
        timeframe: str | None = None,
        action: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ResearchRunSummary], int]:
        rows = self._read_index()
        if asset_id:
            rows = [r for r in rows if r.asset_id == asset_id.upper()]
        if timeframe:
            rows = [r for r in rows if r.timeframe == timeframe]
        if action:
            rows = [r for r in rows if r.signal_action == action.upper()]
        if date_from:
            rows = [r for r in rows if r.generated_at >= date_from]
        if date_to:
            rows = [r for r in rows if r.generated_at <= date_to]
        total = len(rows)
        return rows[offset : offset + limit], total

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        """Full stored payload: summary + report (+ signal when present)."""
        if not run_id.replace("-", "").isalnum() or "/" in run_id or ".." in run_id:
            return None  # path-safety: run ids are our own tokens
        run_dir = self._root / run_id
        report_path = run_dir / "report.json"
        if not report_path.exists():
            return None
        payload: dict[str, Any] = {"run_id": run_id}
        payload["report"] = json.loads(report_path.read_text(encoding="utf-8"))
        signal_path = run_dir / "signal.json"
        if signal_path.exists():
            payload["signal"] = json.loads(signal_path.read_text(encoding="utf-8"))
        for row in self._read_index():
            if row.run_id == run_id:
                payload["summary"] = row.model_dump(mode="json")
                break
        return payload


class PersistingResearchRunner:
    """``ResearchRunner`` for the paper engine that also archives reports.

    Subclasses the production adapter so behaviour stays identical; only the
    persistence/event side effects are added. Implements RunnerProtocol.
    """

    prompt_version = "phase1-research-engine"

    def __init__(
        self,
        *,
        artifact_store: ResearchArtifactStore,
        bus: EventBus | None = None,
        provider: Any,
        now_fn: Callable[[], datetime],
        disabled_components: tuple[str, ...] = ("news", "sentiment"),
        research_config: dict[str, Any] | None = None,
        enable_macro: bool = False,
    ):
        from tradingagents.paper.engine import LiveResearchRunner

        self._inner = LiveResearchRunner(
            provider=provider,
            now_fn=now_fn,
            disabled_components=disabled_components,
            research_config=research_config,
            enable_macro=enable_macro,
        )
        self._artifacts = artifact_store
        self._bus = bus

    @property
    def model_ids(self) -> list[str]:
        return self._inner.model_ids

    @property
    def config_hash(self) -> str:
        return self._inner.config_hash

    def run(self, asset_id: str, timeframe: str) -> tuple[Any, str]:
        if self._bus is not None:
            self._bus.notify(
                "research_started",
                {"asset_id": asset_id, "timeframe": timeframe},
            )
        try:
            result = self._inner._engine.run(asset_id, timeframe)
        except Exception as exc:
            if self._bus is not None:
                self._bus.notify(
                    "research_failed",
                    {
                        "asset_id": asset_id,
                        "timeframe": timeframe,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                )
            raise
        summary = self._artifacts.save_result(
            result, model_ids=list(self.model_ids)
        )
        if self._bus is not None:
            self._bus.notify(
                "research_completed",
                {
                    "run_id": summary.run_id,
                    "asset_id": summary.asset_id,
                    "timeframe": summary.timeframe,
                    "signal_action": summary.signal_action,
                    "confidence": summary.confidence,
                },
            )
        return result.signal, result.no_signal_reason


__all__ = [
    "PersistingResearchRunner",
    "ResearchArtifactStore",
    "ResearchRunSummary",
]
