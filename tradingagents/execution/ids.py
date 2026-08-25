"""Deterministic client order ids + idempotency index (P5 §17/§18).

Format: ``TA-{env}-{account}-{signal8}-{seq}`` where ``signal8`` is the
first 8 hex of the signal id and ``seq`` is the per-signal attempt counter
recorded in the idempotency index (persisted, restart-safe). The same
logical order therefore always maps to the same client id on retry-after-
reconcile, while a genuinely new submission for the same signal gets the
next sequence — and only reconciliation may authorize that next sequence.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from tradingagents.execution.store import ExecutionStore


class IdempotencyEntry(BaseModel):
    signal_id: str
    client_order_ids: list[str]
    quarantined: bool = False


class IdempotencyIndex:
    def __init__(self, store: ExecutionStore) -> None:
        self._store = store
        self._entries: dict[str, IdempotencyEntry] = {}
        self._load()

    def _load(self) -> None:
        data = self._store.root / "idempotency.json"
        if not data.exists():
            return
        try:
            raw = json.loads(data.read_text(encoding="utf-8"))
            self._entries = {k: IdempotencyEntry.model_validate(v) for k, v in raw.items()}
        except (OSError, ValueError) as exc:
            from tradingagents.execution.store import ExecutionStoreError

            raise ExecutionStoreError(f"corrupt idempotency index: {exc}") from exc

    def _flush(self) -> None:
        path = self._store.root / "idempotency.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({k: json.loads(v.model_dump_json()) for k, v in self._entries.items()}, indent=1),
            encoding="utf-8",
        )
        tmp.replace(path)

    def client_order_id(self, *, environment: str, account_id: str, signal_id: str) -> str:
        entry = self._entries.get(signal_id)
        seq = len(entry.client_order_ids) + 1 if entry else 1
        signal8 = signal_id.replace("-", "")[:8]
        return f"TA-{environment}-{account_id}-{signal8}-{seq}"

    def register_submission(self, *, signal_id: str, client_order_id: str) -> None:
        entry = self._entries.setdefault(
            signal_id, IdempotencyEntry(signal_id=signal_id, client_order_ids=[])
        )
        if client_order_id in entry.client_order_ids:
            return  # re-registration after reconcile is a no-op
        entry.client_order_ids.append(client_order_id)
        self._flush()

    def submissions_for(self, signal_id: str) -> list[str]:
        entry = self._entries.get(signal_id)
        return list(entry.client_order_ids) if entry else []

    def set_quarantined(self, signal_id: str, quarantined: bool) -> None:
        entry = self._entries.get(signal_id)
        if entry is None or entry.quarantined == quarantined:
            return
        entry.quarantined = quarantined
        self._flush()

    def is_quarantined(self, signal_id: str) -> bool:
        entry = self._entries.get(signal_id)
        return entry.quarantined if entry else False
