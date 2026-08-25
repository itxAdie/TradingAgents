"""LiveExecutionEngine: signal → validation → policy → hard gate → adapter
submission with idempotency, UNKNOWN quarantine, reconciliation and metrics
(P5 §5/§18/§19/§56).

Fail-closed everywhere: any exception in any layer blocks the cycle and is
audited. The engine never retries an order whose submission outcome was
lost — it quarantines the signal, trips the breaker, alerts and waits for
reconciliation to resolve the true state.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

from tradingagents.api.eventbus import EventBus
from tradingagents.assets.registry import get_asset
from tradingagents.brokers.base import (
    BrokerAdapter,
    BrokerError,
    BrokerOrderInfo,
    ConnectionStatus,
    ErrorClass,
)
from tradingagents.execution.alerts import (
    ALERT_BROKER_DISCONNECTED,
    ALERT_CIRCUIT_BREAKER_TRIGGERED,
    ALERT_DATABASE_FAILURE,
    ALERT_EXCESSIVE_SLIPPAGE,
    ALERT_LIVE_TRADING_HALTED,
    ALERT_ORDER_REJECTED,
    ALERT_ORDER_UNKNOWN,
    ALERT_POSITION_MISMATCH,
    ALERT_RISK_LIMIT_REACHED,
    AlertEmitter,
)
from tradingagents.execution.config import LiveExecutionConfig
from tradingagents.execution.gate import CircuitBreaker, GateDecision, HardRiskGate
from tradingagents.execution.ids import IdempotencyIndex
from tradingagents.execution.models import (
    TERMINAL_LIVE_STATES,
    FillRecord,
    LiveOrder,
    LiveOrderEvent,
    LiveOrderState,
    LivePosition,
    ReconciliationReport,
)
from tradingagents.execution.monitor import MetricsRegistry
from tradingagents.execution.policy import ExecutionPolicy, PolicyDecision
from tradingagents.execution.reconcile import ReconciliationEngine
from tradingagents.execution.store import ExecutionStore, ExecutionStoreError
from tradingagents.marketdata.models import OhlcvSeries
from tradingagents.marketdata.provider import MarketDataProvider
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.paper.events import NotificationProvider
from tradingagents.research.schemas import ResearchSignal, SignalAction

CycleOutcome = Literal[
    "submitted",
    "no_trade",
    "blocked",
    "rejected_by_broker",
    "quarantined_unknown",
]

#: broker-reported status -> local state we may fold directly (reconcile.py
#: owns the stricter mapping used during reconciliation passes).
_STATUS_FOLD: dict[str, LiveOrderState] = {
    "ACKNOWLEDGED": LiveOrderState.ACKNOWLEDGED,
    "PARTIALLY_FILLED": LiveOrderState.PARTIALLY_FILLED,
    "FILLED": LiveOrderState.FILLED,
    "CANCELLED": LiveOrderState.CANCELLED,
    "REJECTED": LiveOrderState.REJECTED,
    "EXPIRED": LiveOrderState.EXPIRED,
}


def _noop_audit(action: str, **detail: object) -> None:
    del action, detail


def execution_signal_id(signal: ResearchSignal) -> str:
    """Deterministic content identity for a research signal.

    Mirrors the paper phase's content-based idempotency
    (:mod:`tradingagents.paper.signal_id`): identical decision content always
    yields the same id across restarts and retries, so duplicate protection
    is content-based, never session-based.
    """
    payload = json.dumps(
        {
            "asset_id": signal.asset_id.strip().upper(),
            "timeframe": signal.timeframe.strip().lower(),
            "generated_at": signal.generated_at.astimezone(timezone.utc).isoformat(),
            "action": signal.action.value,
            "confidence": signal.confidence,
            "entry_reference": signal.entry_reference,
            "stop_loss_reference": signal.stop_loss_reference,
            "take_profit_reference": signal.take_profit_reference,
            "thesis": signal.thesis,
            "models_used": sorted(set(signal.models_used)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CycleResult:
    outcome: CycleOutcome
    reason_code: str = ""
    detail: str = ""
    order_id: str | None = None
    client_order_id: str | None = None


@dataclass
class EngineState:
    started: bool = False
    ready: bool = False
    ready_blockers: list[str] = field(default_factory=list)
    consecutive_losses: int = 0
    day_start_equity: float | None = None
    day_anchor_date: date | None = None
    peak_equity: float | None = None
    #: equity-delta proxy for realized P&L today (sandbox scope; see
    #: _refresh_day_anchor). Gate treats None as fail-closed.
    realized_pnl_today: float = 0.0


class LiveExecutionEngine:
    def __init__(
        self,
        *,
        config: LiveExecutionConfig,
        adapter: BrokerAdapter,
        provider: MarketDataProvider,
        notifier: NotificationProvider,
        bus: EventBus | None = None,
        audit: Callable[..., None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._audit_fn = audit or _noop_audit
        self._store = ExecutionStore(config.store_root())
        self._ids = IdempotencyIndex(self._store)
        self._breaker = CircuitBreaker(self._store)
        self._gate = HardRiskGate(
            limits=config.limits, store=self._store, breaker=self._breaker
        )
        self._policy = ExecutionPolicy(config=config, store=self._store)
        self._reconciler = ReconciliationEngine(store=self._store)
        self._metrics = MetricsRegistry()
        self._alerts = AlertEmitter(
            notifier=notifier, bus=bus, environment_tag=config.environment
        )
        self.state = EngineState()

    # -- properties ------------------------------------------------------------

    @property
    def metrics(self) -> MetricsRegistry:
        return self._metrics

    @property
    def store(self) -> ExecutionStore:
        return self._store

    @property
    def adapter(self) -> BrokerAdapter:
        """Read-only adapter access for operator tooling (CLI connect-first)."""
        return self._adapter

    @property
    def ready(self) -> bool:
        return self.state.ready

    def status_snapshot(self) -> dict[str, object]:
        """Dashboard/CLI-facing status; never raises."""
        conn = ConnectionStatus.UNKNOWN
        with contextlib.suppress(Exception):
            conn = self._adapter.health_check()
        account_ok = False
        with contextlib.suppress(Exception):
            snap = self._adapter.get_account()
            account_ok = snap.account_id == self._config.account_id
        last_recon: dict[str, object] | None = None
        try:
            report = self._store.last_reconciliation()
            last_recon = report.model_dump(mode="json") if report else None
        except ExecutionStoreError:
            pass
        halted, halt_reason = self._store.is_halted()
        tripped, trip_reason = self._store.circuit_breaker_state()
        return {
            "environment": self._config.environment,
            "broker": self._config.broker_name,
            "account_id": self._config.account_id,
            "account_verified": account_ok,
            "connection": conn.value,
            "ready": self.state.ready,
            "halted": halted,
            "halt_reason": halt_reason,
            "circuit_breaker": tripped,
            "circuit_breaker_reason": trip_reason,
            "configuration_version": self._config.configuration_version(),
            "live_armed": self._config.live_armed,
            "last_reconciliation": last_recon,
            "metrics": self._metrics.snapshot(),
        }

    # -- startup / shutdown ------------------------------------------------------

    def startup(self) -> tuple[bool, list[str]]:
        """Restart-safety sequence; execution stays blocked until fully green.

        Order matters (spec §21): connect → account identity check → full
        reconciliation BEFORE any order can pass the policy chain.
        """
        blockers: list[str] = []
        self.state.started = True
        try:
            status = self._adapter.connect()
        except Exception as exc:  # noqa: BLE001
            blockers.append(f"connect_failed:{exc}")
            status = ConnectionStatus.UNKNOWN
        if status is not ConnectionStatus.CONNECTED:
            blockers.append(f"connection_{status.value}")
            self._alerts.emit(ALERT_BROKER_DISCONNECTED, {"detail": status.value})
        else:
            try:
                snap = self._adapter.get_account()
                if snap.account_id != self._config.account_id:
                    blockers.append(
                        f"account_mismatch:configured={self._config.account_id}"
                        f":broker={snap.account_id}"
                    )
                else:
                    self.state.day_start_equity = snap.equity
                    self.state.day_anchor_date = self._clock().date()
                    self.state.peak_equity = max(
                        snap.equity, self.state.peak_equity or snap.equity
                    )
                    self._metrics.gauge("equity", snap.equity)
            except Exception as exc:  # noqa: BLE001
                blockers.append(f"account_unavailable:{exc}")

            try:
                report = self._reconciler.run(
                    adapter=self._adapter,
                    trigger="startup",
                    local_orders=self._store.all_order_snapshots(),
                    now=self._clock(),
                )
                if not report.clean:
                    blockers.append(f"reconciliation_dirty:{len(report.mismatches)}")
                    self._emit_position_mismatch(report)
            except ExecutionStoreError as exc:
                blockers.append(f"store_failure:{exc}")
                self._alerts.emit(ALERT_DATABASE_FAILURE, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                blockers.append(f"reconciliation_failed:{exc}")
                self._trip_breaker("startup reconciliation failed", str(exc))

        self.state.ready_blockers = blockers
        self.state.ready = not blockers
        self._metrics.heartbeat("execution_engine", at=self._clock())
        self._audit_fn(
            "execution_startup", ready=self.state.ready, blockers=list(blockers)
        )
        return self.state.ready, list(blockers)

    def shutdown(self) -> dict[str, object]:
        """Best-effort final reconciliation + clean disconnect."""
        summary: dict[str, object] = {}
        try:
            open_orders = [
                o
                for o in self._store.all_order_snapshots()
                if o.status not in TERMINAL_LIVE_STATES
            ]
            summary["open_orders_left"] = len(open_orders)
            report = self._reconciler.run(
                adapter=self._adapter,
                trigger="manual",
                local_orders=self._store.all_order_snapshots(),
                now=self._clock(),
            )
            summary["final_reconciliation_clean"] = report.clean
        except Exception as exc:  # noqa: BLE001 - best effort on shutdown
            summary["final_reconciliation_error"] = str(exc)
        finally:
            with contextlib.suppress(Exception):
                self._adapter.disconnect()
        self.state.ready = False
        self._audit_fn("execution_shutdown", **summary)
        return summary

    # -- halts ---------------------------------------------------------------------

    def halt(self, reason: str, *, operator: str = "operator") -> None:
        self._store.set_halt(True, reason, operator=operator)
        self._alerts.emit(
            ALERT_LIVE_TRADING_HALTED, {"reason": reason, "operator": operator}
        )
        self._audit_fn("execution_halt", reason=reason, operator=operator)

    def resume(self, *, operator: str) -> None:
        """Manual-only resume of a halt and/or circuit breaker."""
        tripped, _ = self._store.circuit_breaker_state()
        if tripped:
            self._breaker.reset(operator=operator)
        self._store.set_halt(False, f"resumed by {operator}", operator=operator)
        self._audit_fn("execution_resume", operator=operator)

    # -- main cycle -------------------------------------------------------------------

    def run_cycle(self, signal: ResearchSignal) -> CycleResult:
        now = self._clock()
        self._metrics.heartbeat("execution_engine", at=now)
        self._metrics.inc("cycles_total")

        if not self.state.started or not self.state.ready:
            return CycleResult(
                "blocked", "engine_not_ready", "; ".join(self.state.ready_blockers)
            )

        if signal.action is SignalAction.HOLD:
            return CycleResult("no_trade", "hold")

        sig_id = execution_signal_id(signal)
        if self._ids.is_quarantined(sig_id):
            return CycleResult(
                "blocked",
                "signal_quarantined",
                "prior submission unresolved; reconciliation must clear it first",
            )
        # §18: a repeat delivery of a signal with an in-flight submission is
        # blocked (restart-safe: the idempotency index + snapshots persist).
        in_flight = [
            cid
            for cid in self._ids.submissions_for(sig_id)
            if self._submission_open(cid)
        ]
        if in_flight:
            return CycleResult(
                "blocked",
                "signal_in_flight",
                f"prior submission(s) still open: {', '.join(in_flight)}",
            )

        connection, account = self._probe_broker()

        decision: PolicyDecision = self._policy.check(
            connection=connection,
            adapter_healthy=connection is ConnectionStatus.CONNECTED,
            account=account,
            last_bar_close=self._last_closed_bar(signal.asset_id, signal.timeframe),
            timeframe=signal.timeframe,
            now=now,
        )
        if not decision.allowed:
            return self._blocked(decision.reason_code, decision.detail, signal)

        assert account is not None  # policy guarantees a matched snapshot
        self._refresh_day_anchor(account.equity, now)

        invalid = self._validate_signal(signal)
        if invalid is not None:
            return self._blocked(invalid[0], invalid[1], signal)

        entry_price = float(signal.entry_reference)  # validated non-None above
        quantity = self._risk_budget_quantity(signal, entry_price, account.equity)
        if quantity <= 0:
            return self._blocked(
                "quantity_below_minimum",
                f"risk budget produced quantity {quantity} for {signal.asset_id}",
                signal,
            )

        gate_decision: GateDecision = self._gate.evaluate(
            action=signal.action,
            asset_id=signal.asset_id,
            quantity=quantity,
            reference_price=entry_price,
            stop_distance_pct=self._stop_distance_pct(signal, entry_price),
            account=account,
            connection=connection,
            open_orders_count=len(
                [
                    o
                    for o in self._store.all_order_snapshots()
                    if o.status not in TERMINAL_LIVE_STATES
                ]
            ),
            consecutive_losses=self.state.consecutive_losses,
            day_start_equity=self.state.day_start_equity,
            peak_equity=self.state.peak_equity,
            realized_pnl_today=self.state.realized_pnl_today,
        )
        if not gate_decision.approved:
            self._metrics.inc("risk_blocks_total")
            if gate_decision.reason_code.startswith(("max_", "daily", "drawdown")):
                self._alerts.emit(
                    ALERT_RISK_LIMIT_REACHED,
                    {"reason_code": gate_decision.reason_code, "detail": gate_decision.detail},
                )
            return self._blocked(gate_decision.reason_code, gate_decision.detail, signal)

        return self._submit(order_request=(signal, entry_price, quantity), now=now)

    # -- submission path ---------------------------------------------------------------

    def _submit(
        self,
        *,
        order_request: tuple[ResearchSignal, float, float],
        now: datetime,
    ) -> CycleResult:
        signal, entry_price, quantity = order_request
        sig_id = execution_signal_id(signal)
        client_order_id = self._ids.client_order_id(
            environment=self._config.environment,
            account_id=self._config.account_id,
            signal_id=sig_id,
        )
        order = LiveOrder(
            order_id=str(uuid.uuid4()),
            client_order_id=client_order_id,
            account_id=self._config.account_id,
            environment=self._config.environment,
            asset_id=signal.asset_id,
            timeframe=signal.timeframe,
            side=signal.action.value,  # type: ignore[arg-type]
            order_type="MARKET",
            quantity=quantity,
            stop_loss=signal.stop_loss_reference,
            take_profit=signal.take_profit_reference,
            reference_price=entry_price,
            signal_id=sig_id,
            strategy_version=f"ai_research@{self._config.configuration_version()}",
            research_version=",".join(sorted(set(signal.models_used))) or "n/a",
            configuration_version=self._config.configuration_version(),
            risk_configuration_version=self._config.configuration_version(),
            created_at=now,
            updated_at=now,
        )
        self._store.save_order_snapshot(order)
        order = self._transition(order, LiveOrderState.RISK_APPROVED, "hard_gate_approved", now)

        t_submit = self._clock()
        try:
            outcome = self._adapter.submit_order(
                client_order_id=client_order_id,
                asset_id=order.asset_id,
                side=order.side,
                order_type="MARKET",
                quantity=quantity,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
            )
        except BrokerError as exc:
            self._metrics.record_sample(
                "submit_latency_s", (self._clock() - t_submit).total_seconds()
            )
            return self._handle_broker_error(order, exc)
        except Exception as exc:  # noqa: BLE001 - transport failure ⇒ UNKNOWN
            return self._mark_unknown(order, f"transport failure: {exc}")

        self._metrics.record_sample(
            "submit_latency_s", (self._clock() - t_submit).total_seconds()
        )
        self._metrics.inc("orders_submitted_total")
        self._ids.register_submission(
            signal_id=sig_id, client_order_id=client_order_id
        )
        order = self._transition(order, LiveOrderState.SUBMITTED, "sent_to_broker", self._clock())

        if outcome.unknown or outcome.order is None:
            return self._mark_unknown(
                order, outcome.detail or "submission outcome unknown"
            )

        order, filled_delta = self._fold_broker_info(order, outcome.order)
        info = outcome.order
        if info.status.value == "REJECTED":
            self._metrics.inc("orders_rejected_total")
            self._alerts.emit(
                ALERT_ORDER_REJECTED,
                {"client_order_id": client_order_id, "detail": info.raw_status},
            )
        if info.status.value in {"FILLED", "PARTIALLY_FILLED"}:
            self._on_fill_progress(order, filled_delta=filled_delta)
        self._audit_order(order)
        return CycleResult(
            "submitted",
            order.status.value.lower(),
            "",
            order.order_id,
            client_order_id,
        )

    # -- broker update polling ---------------------------------------------------------

    def process_updates(self) -> list[str]:
        """Fold broker-side transitions into non-terminal local orders."""
        notes: list[str] = []
        for order in self._store.all_order_snapshots():
            if order.status in TERMINAL_LIVE_STATES:
                continue
            try:
                info = self._adapter.find_order(client_order_id=order.client_order_id)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{order.order_id}: lookup failed ({exc})")
                continue
            if info is None:
                notes.append(f"{order.order_id}: absent from broker book")
                continue
            before = order.status
            order, filled_delta = self._fold_broker_info(order, info)
            if order.status is not before:
                notes.append(f"{order.order_id}: {before.value} -> {order.status.value}")
            if filled_delta > 1e-12:
                self._on_fill_progress(order, filled_delta=filled_delta)
        self._metrics.heartbeat("execution_engine", at=self._clock())
        return notes

    def reconcile(self, *, trigger: str = "periodic") -> ReconciliationReport:
        try:
            report = self._reconciler.run(
                adapter=self._adapter,
                trigger=trigger,
                local_orders=self._store.all_order_snapshots(),
                now=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001 - unverifiable state ⇒ halt trading
            self._trip_breaker("reconciliation failed", str(exc))
            raise
        if not report.clean:
            self._emit_position_mismatch(report)
            kinds = sorted({m.kind for m in report.mismatches})
            self._trip_breaker("reconciliation mismatch", ", ".join(kinds))
        return report

    # -- internals ------------------------------------------------------------------------

    def _submission_open(self, client_order_id: str) -> bool:
        """True when a prior submission for this client id is not terminal."""
        for order in self._store.all_order_snapshots():
            if order.client_order_id == client_order_id:
                return order.status not in TERMINAL_LIVE_STATES
        return True  # unknown fate ⇒ treat as open; reconciliation decides

    def _probe_broker(self) -> tuple[ConnectionStatus, object | None]:
        try:
            connection = self._adapter.health_check()
        except Exception as exc:  # noqa: BLE001 - policy fails closed on UNKNOWN
            self._audit_fn("execution_health_probe_failed", detail=str(exc))
            return ConnectionStatus.UNKNOWN, None
        if connection is not ConnectionStatus.CONNECTED:
            return connection, None
        try:
            return connection, self._adapter.get_account()
        except Exception as exc:  # noqa: BLE001
            self._audit_fn("execution_account_probe_failed", detail=str(exc))
            return connection, None

    def _blocked(self, code: str, detail: str, signal: ResearchSignal) -> CycleResult:
        self._metrics.inc("risk_blocks_total")
        self._audit_fn(
            "execution_blocked",
            reason=code,
            detail=detail,
            signal_id=execution_signal_id(signal),
        )
        return CycleResult("blocked", code, detail)

    def _handle_broker_error(self, order: LiveOrder, exc: BrokerError) -> CycleResult:
        self._metrics.inc("broker_errors_total")
        if exc.classification is ErrorClass.UNKNOWN:
            # outcome lost: quarantine + reconcile, never retry (§19)
            return self._mark_unknown(order, f"{exc.code}: {exc.message}")
        now = self._clock()
        failed = self._transition(
            order, LiveOrderState.REJECTED, f"{exc.classification.value}:{exc.code}", now
        )
        self._metrics.inc("orders_rejected_total")
        self._alerts.emit(
            ALERT_ORDER_REJECTED,
            {"client_order_id": order.client_order_id, "detail": exc.message},
        )
        self._audit_order(failed)
        return CycleResult(
            "rejected_by_broker",
            exc.code,
            exc.message,
            failed.order_id,
            failed.client_order_id,
        )

    def _mark_unknown(self, order: LiveOrder, detail: str) -> CycleResult:
        now = self._clock()
        unknown = self._transition(order, LiveOrderState.UNKNOWN, detail, now)
        self._ids.set_quarantined(order.signal_id, True)
        self._metrics.inc("orders_unknown_total")
        self._alerts.emit(
            ALERT_ORDER_UNKNOWN,
            {"client_order_id": order.client_order_id, "detail": detail},
        )
        self._trip_breaker("unknown submission state", detail)
        self._audit_order(unknown)
        return CycleResult(
            "quarantined_unknown",
            "unknown_state",
            detail,
            unknown.order_id,
            unknown.client_order_id,
        )

    def _trip_breaker(self, reason: str, detail: str) -> None:
        already_tripped, _ = self._store.circuit_breaker_state()
        self._breaker.trip(f"{reason}: {detail}")
        if not already_tripped:
            self._metrics.inc("circuit_breakers_total")
            self._alerts.emit(
                ALERT_CIRCUIT_BREAKER_TRIGGERED, {"reason": reason, "detail": detail}
            )

    def _fold_broker_info(
        self, order: LiveOrder, info: BrokerOrderInfo
    ) -> tuple[LiveOrder, float]:
        """Fold broker state into the local view; returns (order, executed delta)."""
        target = _STATUS_FOLD.get(info.status.value)
        if target is None:
            return order, 0.0  # unmappable status: reconciliation flags it
        delta = info.filled_quantity - order.filled_quantity
        if delta > 1e-12:
            fill_price = info.avg_fill_price or 0.0
            fill = FillRecord(
                quantity=round(delta, 10),
                price=max(fill_price, 1e-9),  # FillRecord requires price > 0
                ts=info.ts or self._clock(),
                fee=info.fees_reported,
                fee_pending=info.fees_reported is None,
                reference_price=order.reference_price,
            )
            merged = order.model_copy(update={"fills": [*order.fills, fill]})
            self._store.append_fill(order.order_id, fill)
        else:
            merged = order
        merged = merged.model_copy(
            update={
                "broker_order_id": info.broker_order_id or merged.broker_order_id,
                "stop_loss": info.stop_loss_attached or merged.stop_loss,
                "take_profit": info.take_profit_attached or merged.take_profit,
            }
        )
        if merged.status is not target:
            event = LiveOrderEvent(
                ts=self._clock(),
                order_id=merged.order_id,
                client_order_id=merged.client_order_id,
                account_id=merged.account_id,
                signal_id=merged.signal_id,
                from_state=merged.status,
                to_state=target,
                reason="broker_update",
                broker_order_id=info.broker_order_id or merged.broker_order_id,
            )
            merged = merged.with_transition(
                new_state=target, reason="broker_update", at=event.ts
            )
            self._store.append_order_event(event)
        elif merged is not order:
            merged = merged.model_copy(update={"updated_at": self._clock()})
        self._store.save_order_snapshot(merged)
        return merged, delta

    def _on_fill_progress(self, order: LiveOrder, *, filled_delta: float) -> None:
        """Metrics, P&L proxies, position cache and post-trade reconciliation."""
        self._metrics.inc("orders_filled_total" if order.filled_quantity >= order.quantity else "partial_fills_total")
        if order.submitted_at and order.acknowledged_at:
            self._metrics.record_sample(
                "ack_latency_s",
                (order.acknowledged_at - order.submitted_at).total_seconds(),
            )
        if order.acknowledged_at and order.filled_at:
            self._metrics.record_sample(
                "fill_latency_s",
                (order.filled_at - order.acknowledged_at).total_seconds(),
            )
        fees = sum(f.fee or 0.0 for f in order.fills)
        if fees:
            self._metrics.gauge("fees_last_order", fees)
        for fill in order.fills:
            slip = fill.slippage_bps
            if slip is not None and abs(slip) > self._config.max_slippage_bps:
                self._metrics.inc("excessive_slippage_total")
                self._alerts.emit(
                    ALERT_EXCESSIVE_SLIPPAGE,
                    {
                        "client_order_id": order.client_order_id,
                        "slippage_bps": round(slip, 2),
                        "threshold_bps": self._config.max_slippage_bps,
                    },
                )
        self._update_position_cache(order, filled_delta)
        if order.status is LiveOrderState.FILLED:
            self._track_closed_trade(order)
            try:
                self._reconciler.run(
                    adapter=self._adapter,
                    trigger="post_trade",
                    local_orders=[order],
                    now=self._clock(),
                )
            except Exception as exc:  # noqa: BLE001
                self._trip_breaker("post-trade reconciliation failed", str(exc))
            self._probe_equity_gauge()

    def _update_position_cache(self, order: LiveOrder, filled_delta: float) -> None:
        """Maintain the broker-comparison cache so post_trade recon is meaningful.

        Applies only the newly-executed quantity so repeated folds of the same
        order never double-count, and restarts re-fold without side effects.
        """
        if abs(filled_delta) < 1e-12:
            return
        signed = filled_delta if order.side == "BUY" else -filled_delta
        positions = {p.asset_id: p for p in self._store.load_positions()}
        existing = positions.get(order.asset_id)
        if existing is None:
            new_qty = signed
            avg_entry = order.avg_fill_price or order.reference_price or 0.0
        else:
            new_qty = round(existing.quantity + signed, 10)
            avg_entry = (
                order.avg_fill_price
                if abs(existing.quantity) < 1e-12 or (existing.quantity > 0) == (signed > 0)
                else existing.avg_entry_price
            )
        others = [p for p in self._store.load_positions() if p.asset_id != order.asset_id]
        if abs(new_qty) < 1e-12:
            self._store.save_positions(others)
            return
        others.append(
            LivePosition(
                asset_id=order.asset_id,
                quantity=new_qty,
                avg_entry_price=max(avg_entry, 1e-9),
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                protective_orders_ok=order.stop_loss is not None,
                updated_at=self._clock(),
            )
        )
        self._store.save_positions(others)

    def _track_closed_trade(self, order: LiveOrder) -> None:
        """Consecutive-loss counter from closing fills vs cached avg entry.

        Honest sandbox-scope proxy: only fills that *reduce* an opposite-side
        cached position produce a closed-trade verdict; opening trades never
        touch the counter.
        """
        positions = {p.asset_id: p for p in self._store.load_positions()}
        pos = positions.get(order.asset_id)
        closing_side = pos is not None and (pos.quantity > 0) == (order.side == "SELL")
        if not closing_side:
            return
        avg = order.avg_fill_price or 0.0
        entry = pos.avg_entry_price if pos else 0.0
        direction = 1.0 if order.side == "SELL" else -1.0  # long-close profit when >0
        realized = direction * (avg - entry) * order.quantity
        self.state.realized_pnl_today += realized
        self._metrics.gauge("realized_pnl_today", self.state.realized_pnl_today)
        if realized < 0:
            self.state.consecutive_losses += 1
        elif realized > 0:
            self.state.consecutive_losses = 0

    def _validate_signal(self, signal: ResearchSignal) -> tuple[str, str] | None:
        try:
            get_asset(signal.asset_id)
        except KeyError as exc:
            return ("unsupported_asset", str(exc))
        if signal.entry_reference is None or signal.entry_reference <= 0:
            return ("invalid_entry", f"entry_reference={signal.entry_reference}")
        if signal.stop_loss_reference is None or signal.stop_loss_reference <= 0:
            return ("missing_stop_level", "mandatory stop missing")
        long_side = signal.action is SignalAction.BUY
        if long_side and signal.stop_loss_reference >= signal.entry_reference:
            return ("invalid_stop_side", "long stop must sit below entry")
        if not long_side and signal.stop_loss_reference <= signal.entry_reference:
            return ("invalid_stop_side", "short stop must sit above entry")
        return None

    def _stop_distance_pct(self, signal: ResearchSignal, entry: float) -> float | None:
        if signal.stop_loss_reference is None or entry <= 0:
            return None
        return abs(entry - signal.stop_loss_reference) / entry * 100

    def _risk_budget_quantity(
        self, signal: ResearchSignal, entry: float, equity: float
    ) -> float:
        """equity × risk% ÷ stop distance, then hard value caps — deterministic."""
        stop_pct = self._stop_distance_pct(signal, entry) or 0.0
        limits = self._config.limits
        # 1bp shaving keeps float drift from tripping the hard gate's
        # `risk > budget` comparison for at-limit orders.
        budget = equity * limits.max_risk_per_trade_pct / 100 * 0.9999
        raw_notional = budget / (stop_pct / 100) if stop_pct > 0 else 0.0
        raw_qty = raw_notional / entry if entry > 0 else 0.0
        cap_value = min(limits.max_order_value, limits.max_position_notional)
        cap_qty = cap_value / entry if entry > 0 else 0.0
        return round(min(raw_qty, cap_qty), 8)

    def _last_closed_bar(self, asset_id: str, timeframe: str) -> datetime | None:
        try:
            spec = get_asset(asset_id)
            series: OhlcvSeries = self._provider.get_ohlcv(
                spec, Timeframe(timeframe), limit=1
            )
            return series.latest_timestamp
        except Exception:  # noqa: BLE001 - freshness treats this as missing data
            return None

    def _refresh_day_anchor(self, equity: float, now: datetime) -> None:
        """UTC-day rollover: re-anchor daily loss budget at current equity."""
        if self.state.day_anchor_date != now.date():
            self.state.day_anchor_date = now.date()
            self.state.day_start_equity = equity
            self.state.realized_pnl_today = 0.0

    def _probe_equity_gauge(self) -> None:
        try:
            snap = self._adapter.get_account()
            self._last_equity = snap.equity
            self._metrics.gauge("equity", snap.equity)
            self.state.peak_equity = max(
                snap.equity, self.state.peak_equity or snap.equity
            )
        except Exception:  # noqa: BLE001 - gauge refresh is best-effort
            pass

    def _emit_position_mismatch(self, report: ReconciliationReport) -> None:
        kinds = sorted({m.kind for m in report.mismatches})
        positional = [k for k in kinds if "POSITION" in k]
        event = ALERT_POSITION_MISMATCH if positional else ALERT_ORDER_UNKNOWN
        self._alerts.emit(event, {"mismatches": kinds, "trigger": report.trigger})

    def _transition(
        self, order: LiveOrder, new_state: LiveOrderState, reason: str, at: datetime
    ) -> LiveOrder:
        """Immutable transition + append-only event, in that order of truth."""
        event = LiveOrderEvent(
            ts=at,
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            account_id=order.account_id,
            signal_id=order.signal_id,
            from_state=order.status,
            to_state=new_state,
            reason=reason[:500],
            broker_order_id=order.broker_order_id,
        )
        updated = order.with_transition(new_state=new_state, reason=reason[:500], at=at)
        self._store.append_order_event(event)
        self._store.save_order_snapshot(updated)
        return updated

    def _audit_order(self, order: LiveOrder) -> None:
        self._audit_fn(
            "live_order_update",
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            asset=order.asset_id,
            side=order.side,
            quantity=order.quantity,
            status=order.status.value,
            reason=order.reason[:200],
            environment=order.environment,
        )
