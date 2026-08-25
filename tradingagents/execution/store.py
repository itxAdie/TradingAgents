"""Append-only persistence for live execution (P5).

Layout under ``<cache>/live/<environment>/<account>/``:
- ``orders.jsonl``       one LiveOrderEvent per transition (append-only)
- ``fills.jsonl``        fill records as reported by the broker
- ``reconciliations.jsonl`` ReconciliationReport lines
- ``state.json``         folded current view: orders index + positions cache
- ``halts.json``         operator halt state + history
- ``circuit_breaker.json`` breaker state
- ``activations.json``   operator activation records (live only)

Same durability rules as the paper stores: atomic writes via temp+rename,
loud corruption errors, never a silent reset. Finalized trade facts are
never rewritten — corrections arrive as new reconciliation events.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from tradingagents.execution.models import (
    FillRecord,
    LiveOrder,
    LiveOrderEvent,
    LiveOrderState,
    LivePosition,
    ReconciliationReport,
)

_STATE_VERSION = 1


class ExecutionStoreError(RuntimeError):
    """Raised on missing/corrupt persistence; callers must fail closed."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ExecutionStoreError(f"cannot create store root {self.root}: {exc}") from exc

    # -- low-level helpers -------------------------------------------------------

    def _jsonl_path(self, name: str) -> Path:
        return self.root / f"{name}.jsonl"

    def append_jsonl(self, name: str, model: object) -> None:
        path = self._jsonl_path(name)
        line = model.model_dump_json() if hasattr(model, "model_dump_json") else json.dumps(model)
        tmp = path.with_suffix(".tmp")
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            raise ExecutionStoreError(f"append failed for {path}: {exc}") from exc

    def read_jsonl(self, name: str, model_cls: type) -> list[object]:
        path = self._jsonl_path(name)
        if not path.exists():
            return []
        out: list[object] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(model_cls.model_validate_json(line))
        except (OSError, ValidationError) as exc:
            raise ExecutionStoreError(f"corrupt {path}: {exc}") from exc
        return out

    def _write_json(self, name: str, payload: dict[str, object]) -> None:
        path = self.root / f"{name}.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"version": _STATE_VERSION, **payload}, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise ExecutionStoreError(f"write failed for {path}: {exc}") from exc

    def _read_json(self, name: str) -> dict[str, object]:
        path = self.root / f"{name}.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutionStoreError(f"corrupt {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ExecutionStoreError(f"unexpected content in {path}")
        if data.get("version") != _STATE_VERSION:
            raise ExecutionStoreError(f"unsupported state version in {path}")
        return data

    # -- orders ---------------------------------------------------------------------

    def append_order_event(self, event: LiveOrderEvent) -> None:
        self.append_jsonl("orders", event)

    def order_events(self) -> list[LiveOrderEvent]:
        return self.read_jsonl("orders", LiveOrderEvent)  # type: ignore[return-value]

    def fold_orders(self) -> dict[str, LiveOrder]:
        """Rebuild current order views from the append-only log."""
        views: dict[str, LiveOrder] = {}
        for ev in self.order_events():
            existing = views.get(ev.order_id)
            if existing is None:
                continue  # creation events are written as full snapshots below
            views[ev.order_id] = existing.with_transition(
                new_state=ev.to_state, reason=ev.reason, at=ev.ts
            )
        return views

    def save_order_snapshot(self, order: LiveOrder) -> None:
        """Persist a full snapshot (used at CREATED so folding can start)."""
        self._write_json(f"order-{order.order_id}", {"order": json.loads(order.model_dump_json())})

    def load_order_snapshot(self, order_id: str) -> LiveOrder | None:
        data = self._read_json(f"order-{order_id}")
        raw = data.get("order")
        if raw is None:
            return None
        return LiveOrder.model_validate(raw)

    def all_order_snapshots(self) -> list[LiveOrder]:
        out: list[LiveOrder] = []
        for path in sorted(self.root.glob("order-*.json")):
            data = self._read_json(path.stem)
            raw = data.get("order")
            if raw is not None:
                out.append(LiveOrder.model_validate(raw))
        return out

    # -- fills ------------------------------------------------------------------------

    def append_fill(self, order_id: str, fill: FillRecord) -> None:
        self.append_jsonl("fills", {"order_id": order_id, **json.loads(fill.model_dump_json())})

    # -- positions ----------------------------------------------------------------------

    def save_positions(self, positions: list[LivePosition]) -> None:
        self._write_json("positions", {"positions": [json.loads(p.model_dump_json()) for p in positions]})

    def load_positions(self) -> list[LivePosition]:
        data = self._read_json("positions")
        return [LivePosition.model_validate(p) for p in data.get("positions", [])]

    # -- reconciliations -------------------------------------------------------------------

    def append_reconciliation(self, report: ReconciliationReport) -> None:
        self.append_jsonl("reconciliations", report)

    def last_reconciliation(self) -> ReconciliationReport | None:
        reports = self.read_jsonl("reconciliations", ReconciliationReport)
        return reports[-1] if reports else None  # type: ignore[return-value]

    # -- halts / circuit breaker --------------------------------------------------------------

    def set_halt(self, halted: bool, reason: str, *, operator: str = "system") -> None:
        current = self.halt_state()
        history = list(current.get("history", []))  # type: ignore[arg-type]
        history.append({"halted": halted, "reason": reason, "operator": operator, "ts": _utcnow().isoformat()})
        self._write_json("halts", {"halted": halted, "reason": reason, "history": history})

    def halt_state(self) -> dict[str, object]:
        return self._read_json("halts")

    def is_halted(self) -> tuple[bool, str]:
        data = self.halt_state()
        return bool(data.get("halted", False)), str(data.get("reason", ""))

    def set_circuit_breaker(self, tripped: bool, reason: str = "") -> None:
        self._write_json("circuit_breaker", {"tripped": tripped, "reason": reason, "ts": _utcnow().isoformat()})

    def circuit_breaker_state(self) -> tuple[bool, str]:
        data = self._read_json("circuit_breaker")
        return bool(data.get("tripped", False)), str(data.get("reason", ""))

    # -- activation ---------------------------------------------------------------------------------

    def save_activation(self, payload: dict[str, object]) -> None:
        self.append_jsonl("activations_raw", payload)
        self._write_json("activation_current", payload)

    def current_activation(self) -> dict[str, object]:
        return self._read_json("activation_current")


__all__ = ["ExecutionStore", "ExecutionStoreError", "LiveOrderState"]
