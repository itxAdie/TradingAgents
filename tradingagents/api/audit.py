"""Append-only audit log for every state-changing dashboard action.

Spec §44 (auditability): starting a backtest, arming the research loop,
adding a journal note — each appends ``{ts, action, detail}`` to
``{cache_dir}/audit.jsonl`` and emits an ``audit_event`` on the bus. The log
never contains secrets; detail payloads are caller-curated identifiers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingagents.api.eventbus import EventBus


class AuditLog:
    """JSONL sink; corruption of one line never breaks reads."""

    def __init__(self, path: Path, bus: EventBus | None = None):
        self._path = Path(path)
        self._bus = bus

    @property
    def path(self) -> Path:
        return self._path

    def record(self, action: str, **detail: Any) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "detail": detail,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        os.chmod(self._path, 0o600)
        if self._bus is not None:
            self._bus.publish({"event": "audit_event", **row})

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-limit:]


__all__ = ["AuditLog"]
