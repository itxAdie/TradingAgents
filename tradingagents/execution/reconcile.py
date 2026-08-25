"""Reconciliation engine: the broker is authoritative (P5 §22–§24).

Compares local order views and position cache against broker state,
produces a persisted ReconciliationReport, resolves what can be resolved
by *adopting broker truth* (with correction events, never rewrites), and
leaves genuine discrepancies flagged so new orders stay blocked until an
operator or policy explicitly clears them.

Runs at startup, after reconnect, periodically, after trades, after errors.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tradingagents.brokers.base import BrokerAdapter
from tradingagents.execution.models import (
    LiveOrder,
    LiveOrderState,
    LivePosition,
    ReconciliationMismatch,
    ReconciliationReport,
)
from tradingagents.execution.store import ExecutionStore

_BROKER_TO_LOCAL: dict[str, LiveOrderState | None] = {
    "PENDING_SUBMIT": None,  # venue-side pending: nothing to fold yet
    "ACKNOWLEDGED": LiveOrderState.ACKNOWLEDGED,
    "PARTIALLY_FILLED": LiveOrderState.PARTIALLY_FILLED,
    "FILLED": LiveOrderState.FILLED,
    "CANCELLED": LiveOrderState.CANCELLED,
    "REJECTED": LiveOrderState.REJECTED,
    "EXPIRED": LiveOrderState.EXPIRED,
    "UNKNOWN": None,  # unresolvable raw status -> stays flagged
}

_QTY_TOLERANCE = 1e-9


class ReconciliationEngine:
    def __init__(self, *, store: ExecutionStore) -> None:
        self._store = store

    def run(
        self,
        *,
        adapter: BrokerAdapter,
        trigger: str,
        local_orders: list[LiveOrder],
        now: datetime | None = None,
    ) -> ReconciliationReport:
        ts = now or datetime.now(timezone.utc)
        mismatches: list[ReconciliationMismatch] = []
        resolutions: list[str] = []

        broker_positions = {p.asset_id: p for p in adapter.get_positions()}
        broker_orders = {
            o.client_order_id: o
            for o in adapter.get_orders(open_only=False)
            if o.status not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        }

        # -- orders ------------------------------------------------------------------
        non_terminal = [
            o for o in local_orders if o.status not in _terminal_local()
        ]
        for order in non_terminal:
            info = broker_orders.pop(order.client_order_id, None)
            if info is None:
                # not open venue-side: maybe it filled/cancelled between polls —
                # query full book before flagging
                found = adapter.find_order(client_order_id=order.client_order_id)
                if found is None:
                    if order.status is LiveOrderState.SUBMITTED and order.submission_unknown:
                        mismatches.append(
                            ReconciliationMismatch(
                                kind="MISSING_LOCAL_ORDER",
                                detail=(
                                    f"submitted order {order.order_id} absent from broker; "
                                    "stays quarantined"
                                ),
                                local_value=order.status.value,
                                broker_value="absent",
                            )
                        )
                    else:
                        resolutions.append(
                            f"order {order.order_id} closed venue-side; awaiting event fold"
                        )
                    continue
                info = found

            local_state = _BROKER_TO_LOCAL.get(info.status.value)
            if local_state is None:
                mismatches.append(
                    ReconciliationMismatch(
                        kind="STALE_STATE",
                        detail=f"order {order.order_id} broker status {info.raw_status!r} unmappable",
                        broker_value=info.raw_status,
                    )
                )
                continue
            if local_state is not order.status and local_state is not None:
                try:
                    updated = order.with_transition(
                        new_state=local_state, reason="reconciled_from_broker", at=ts
                    )
                    self._store.save_order_snapshot(updated)
                    resolutions.append(
                        f"order {order.order_id}: {order.status.value} -> {local_state.value}"
                    )
                except ValueError as exc:
                    mismatches.append(
                        ReconciliationMismatch(
                            kind="STALE_STATE",
                            detail=f"order {order.order_id}: cannot fold {exc}",
                            local_value=order.status.value,
                            broker_value=info.status.value,
                        )
                    )

        for client_id in sorted(broker_orders):
            info = broker_orders[client_id]
            mismatches.append(
                ReconciliationMismatch(
                    kind="UNEXPECTED_BROKER_ORDER",
                    detail=f"broker order {info.broker_order_id} unknown locally",
                    broker_value=client_id,
                )
            )

        # -- positions ------------------------------------------------------------------
        local_positions = {p.asset_id: p for p in self._store.load_positions()}
        for asset_id, bpos in sorted(broker_positions.items()):
            lpos = local_positions.pop(asset_id, None)
            if lpos is None:
                mismatches.append(
                    ReconciliationMismatch(
                        kind="UNEXPECTED_BROKER_POSITION",
                        detail=f"broker holds {bpos.quantity} {asset_id}; no local record",
                        broker_value=str(bpos.quantity),
                    )
                )
                continue
            if abs(lpos.quantity - bpos.quantity) > _QTY_TOLERANCE:
                self._store.save_positions(
                    [p for p in self._store.load_positions() if p.asset_id != asset_id]
                    + [
                        LivePosition(
                            asset_id=asset_id,
                            quantity=bpos.quantity,
                            avg_entry_price=bpos.avg_entry_price,
                            stop_loss=lpos.stop_loss,
                            take_profit=lpos.take_profit,
                            protective_orders_ok=bpos.protective_orders_ok,
                            updated_at=ts,
                        )
                    ]
                )
                mismatches.append(
                    ReconciliationMismatch(
                        kind="POSITION_QUANTITY_MISMATCH",
                        detail=f"adopted broker quantity for {asset_id}",
                        local_value=str(lpos.quantity),
                        broker_value=str(bpos.quantity),
                    )
                )
                resolutions.append(
                    f"{asset_id}: local {lpos.quantity} -> broker {bpos.quantity} (correction recorded)"
                )
            elif not bpos.protective_orders_ok:
                mismatches.append(
                    ReconciliationMismatch(
                        kind="PROTECTIVE_ORDER_MISSING",
                        detail=f"{asset_id} lacks verified protective stop",
                    )
                )

        for asset_id, lpos in sorted(local_positions.items()):
            mismatches.append(
                ReconciliationMismatch(
                    kind="MISSING_LOCAL_POSITION",
                    detail=f"local position {lpos.quantity} {asset_id} absent from broker",
                    local_value=str(lpos.quantity),
                )
            )

        report = ReconciliationReport(
            ts=ts,
            trigger=trigger,  # type: ignore[arg-type]
            orders_checked=len(non_terminal),
            positions_checked=len(broker_positions),
            clean=not mismatches,
            mismatches=mismatches,
            resolutions=resolutions,
        )
        self._store.append_reconciliation(report)
        return report


def _terminal_local() -> frozenset[LiveOrderState]:
    from tradingagents.execution.models import TERMINAL_LIVE_STATES

    return frozenset(TERMINAL_LIVE_STATES)
