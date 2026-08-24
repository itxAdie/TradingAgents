"""Paper-trading engine: the orchestrator (not a second brain).

One cycle = the ARCHITECTURE.md P3.0 pipeline over *actual current market
time*:

    guards → fetch live bars → drop unclosed tail → novelty check →
    reconstruct account + replay newly closed bars through the Phase 2
    ExecutionSimulator (fills pending intents at next-bar-open; resolves
    SL/TP honestly at real historical bars, including bars missed while the
    process was down) → persist accounting → content-based duplicate
    suppression → research (Phase 1 engine) → validation gate → risk vetoes
    → accept + queue intent for the next bar's open → structured events.

Safety properties implemented here:
- kill switch (`PaperTradingConfig.enabled`) and the persisted emergency
  halt flag both block everything;
- entries always fill on a bar that closed *after* the decision bar — never
  on the decision candle itself (Phase 2 semantics reused verbatim);
- no synthetic fallback prices: provider failure fails the cycle loudly;
- restart-safe: pending intents live in the store and are replayed exactly
  once; content-based signal ids make duplicate execution impossible;
- the AI never sizes positions and can never bypass risk controls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import BaseModel, Field

from tradingagents.assets.registry import get_asset
from tradingagents.backtest.execution import ExecutionSimulator
from tradingagents.backtest.ledger import TradeLedger, TradeRecord
from tradingagents.backtest.portfolio import Portfolio, size_position
from tradingagents.marketdata.models import Bar, OhlcvSeries
from tradingagents.marketdata.provider import AssetSpec
from tradingagents.marketdata.timeframes import Timeframe
from tradingagents.paper.config import PaperTradingConfig
from tradingagents.paper.events import (
    EVENT_CYCLE_FAILED,
    EVENT_DUPLICATE_SUPPRESSED,
    EVENT_EMERGENCY_HALT,
    EVENT_EQUITY_SNAPSHOT,
    EVENT_MARKET_DATA_UPDATED,
    EVENT_NO_NEW_BAR,
    EVENT_ORDER_STATE,
    EVENT_PENDING_EXPIRED,
    EVENT_POSITION_CLOSED,
    EVENT_POSITION_OPENED,
    EVENT_RECOVERY_LOADED,
    EVENT_RISK_REJECTED,
    EVENT_SIGNAL_ACCEPTED,
    EVENT_SIGNAL_GENERATED,
    EVENT_SIGNAL_NOT_GENERATED,
    EVENT_STOP_LOSS_TRIGGERED,
    EVENT_TAKE_PROFIT_TRIGGERED,
    EVENT_TRADING_DISABLED,
    EVENT_VALIDATION_REJECTED,
    LoggingNotificationProvider,
    NotificationProvider,
)
from tradingagents.paper.models import (
    AccountState,
    EquitySnapshot,
    JournalEntry,
    OrderState,
    PaperOrderEvent,
    PaperSignalRecord,
    PositionRecord,
    ResearchSnapshot,
    SignalState,
    fold_order_events,
)
from tradingagents.paper.performance import build_daily_row, peak_equity
from tradingagents.paper.report import build_account_report
from tradingagents.paper.risk import RiskEngine
from tradingagents.paper.scheduler import ScheduleKey, next_run_after
from tradingagents.paper.signal_id import (
    compute_config_hash,
    compute_signal_id,
    utc_iso,
    visible_bars_digest,
)
from tradingagents.paper.store import PaperStateStore
from tradingagents.research.logging import log_event
from tradingagents.research.schemas import ResearchSignal, SignalAction

logger = logging.getLogger(__name__)

BARS_REQUESTED = 300


class ResearchRunner(Protocol):
    """Signal-generation seam.

    Production adapter (:class:`LiveResearchRunner`) wraps the unchanged
    Phase 1 ``ResearchEngine``; tests inject deterministic fakes. Returns
    ``(signal | None, no_signal_reason)`` — never fabricates output.
    """

    def run(self, asset_id: str, timeframe: str) -> tuple[ResearchSignal | None, str]: ...


class CycleResult(BaseModel):
    """Serializable outcome of one ``run_cycle`` invocation."""

    status: str
    detail: str = ""
    signal_id: str | None = None
    order_id: str | None = None
    filled: bool = False
    closed_trades: int = 0
    trade_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Market-data wrapper + production research adapter


def closed_slice(series: OhlcvSeries, *, tf_minutes: int, now: datetime) -> list[Bar]:
    """Bars whose interval has fully closed by ``now``.

    Yahoo stamps bars at interval start, so a bar stamped ``T`` carries data
    through ``T + interval``; it counts as closed only once
    ``now >= T + interval``. Effective close = stamp + interval throughout
    the paper package (ARCHITECTURE.md P3.3).
    """
    cutoff = now - timedelta(minutes=tf_minutes)
    return [bar for bar in series.bars if bar.timestamp <= cutoff]


def effective_close(bar: Bar, tf_minutes: int) -> datetime:
    return bar.timestamp + timedelta(minutes=tf_minutes)


class ClosedBarMarketDataProvider:
    """Wraps any provider; hides forming candles from consumers."""

    name = "closed_bar_wrapper"

    def __init__(self, inner: Any, now_fn: Callable[[], datetime]):
        self._inner = inner
        self._now_fn = now_fn

    def get_ohlcv(
        self,
        asset: AssetSpec,
        timeframe: Timeframe,
        *,
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> OhlcvSeries:
        series = self._inner.get_ohlcv(
            asset, timeframe, limit=limit, start=start, end=end
        )
        bars = closed_slice(series, tf_minutes=timeframe.minutes, now=self._now_fn())
        if not bars:
            from tradingagents.dataflows.errors import NoMarketDataError

            raise NoMarketDataError(
                f"no closed {timeframe.value} bars yet for {asset.asset_id}"
            )
        return OhlcvSeries(
            asset_id=series.asset_id,
            timeframe=series.timeframe,
            source=f"closed:{series.source}",
            status=series.status,
            bars=bars,
        )

    def get_quote(self, asset: AssetSpec) -> Any:
        return self._inner.get_quote(asset)


class LiveResearchRunner:
    """Production :class:`ResearchRunner` around the unchanged Phase 1 stack."""

    prompt_version: str = "phase1-research-engine"

    def __init__(
        self,
        *,
        provider: Any,
        llm: Any = None,
        now_fn: Callable[[], datetime],
        disabled_components: tuple[str, ...] = ("news", "sentiment"),
        research_config: dict[str, Any] | None = None,
        enable_macro: bool = False,
    ):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.research.engine import ResearchEngine

        components = list(disabled_components) + ([] if enable_macro else ["macro"])
        config = research_config or DEFAULT_CONFIG.copy()
        self._engine = ResearchEngine(
            config=config,
            provider=ClosedBarMarketDataProvider(provider, now_fn),
            llm=llm,
            now_fn=now_fn,
            disabled_components=tuple(components),
        )
        self.model_ids = [
            str(config.get("quick_think_llm", "")),
            str(config.get("deep_think_llm", "")),
        ]
        self.config_hash = compute_config_hash(config)

    def run(self, asset_id: str, timeframe: str) -> tuple[ResearchSignal | None, str]:
        result = self._engine.run(asset_id, timeframe)
        return result.signal, result.no_signal_reason


# ---------------------------------------------------------------------------
# Engine


class PaperTradingEngine:
    """Drives full paper-trading cycles for one virtual account."""

    def __init__(
        self,
        *,
        config: PaperTradingConfig,
        store: PaperStateStore,
        provider: Any,
        runner: ResearchRunner,
        notifier: NotificationProvider | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.store = store
        self.provider = provider
        self.runner = runner
        self.notifier = notifier or LoggingNotificationProvider()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._risk = RiskEngine(
            limits=config.risk,
            kill_switch_enabled=True,  # guards below handle disabled status text
            is_halted=lambda: (False, ""),
        )

    # -- helpers ---------------------------------------------------------------

    def _emit(self, event: str, **payload: Any) -> None:
        payload.setdefault("account_id", self.config.account_id)
        self.notifier.notify(event, payload)

    @staticmethod
    def _utc(moment: datetime) -> datetime:
        return moment.astimezone(timezone.utc)

    # -- account lifecycle ------------------------------------------------------

    def init_account(self) -> AccountState:
        """Create the virtual account + capital-anchor equity point."""
        now = self._utc(self._now_fn())
        state = AccountState(
            account_id=self.config.account_id,
            environment=self.config.environment,
            initial_capital=self.config.initial_capital,
            cash=self.config.initial_capital,
            created_at=now,
            updated_at=now,
        )
        self.store.create_account(state)
        self.store.save_positions([])
        self.store.append_equity(
            EquitySnapshot(
                timestamp=now,
                equity=state.initial_capital,
                cash=state.initial_capital,
                exposure=0.0,
                open_positions=0,
                drawdown_pct=0.0,
                balance=state.initial_capital,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
            )
        )
        return state

    # -- reconstruction (startup recovery == every cycle's starting point) ------

    def _reconstruct(
        self, asset_id: str, timeframe_value: str
    ) -> tuple[Portfolio, TradeLedger, dict[str, PositionRecord], ExecutionSimulator]:
        state = self.store.load_account()
        records = self.store.load_trades()
        ledger = TradeLedger(run_id=f"paper-{self.config.account_id}")
        ledger.records.extend(records)
        ledger._next_id = len(records) + 1

        portfolio = Portfolio(initial_capital=state.initial_capital)
        portfolio.cash = state.cash
        portfolio.realized_pnl = state.realized_pnl
        portfolio.total_costs_paid = state.total_costs_paid
        portfolio.closed_trades = state.closed_trades

        position_records = {rec.asset_id: rec for rec in self.store.load_positions()}
        for rec in position_records.values():
            portfolio.open_position(rec.to_sim_position())

        sim = ExecutionSimulator(
            run_id=f"paper-{self.config.account_id}",
            strategy_id="ai_research",
            asset_id=asset_id,
            timeframe=timeframe_value,
            timeframe_minutes=Timeframe(timeframe_value).minutes,
            execution_cfg=self.config.execution,
            sizing=self.config.sizing,
            limits=self.config.risk,
            portfolio=portfolio,
            ledger=ledger,
        )
        return portfolio, ledger, position_records, sim

    def _restore_pending(
        self, sim: ExecutionSimulator
    ) -> list[tuple[ResearchSignal, str, str]]:
        """Re-arm the unfilled accepted intent for this slot after downtime.

        Returns ``[(signal_object, signal_id, order_id), ...]`` for
        attribution of the fill that will (re)happen during replay.
        """
        folded = fold_order_events(self.store.load_order_events())
        candidates = [
            order
            for order in folded.values()
            if order.state is OrderState.ACCEPTED
            and order.asset_id == sim.asset_id
            and order.timeframe == sim.timeframe
        ]
        links: list[tuple[ResearchSignal, str, str]] = []
        for order in sorted(candidates, key=lambda o: o.created_at):
            record = self.store.load_signal(order.signal_id)
            if record is None:
                continue
            rebuilt = record.to_research_signal()
            sim.schedule_signal(rebuilt, self._now_fn())
            links.append((rebuilt, record.signal_id, order.order_id))
        if links:
            self._emit(EVENT_RECOVERY_LOADED, pending_restored=len(links))
            log_event(EVENT_RECOVERY_LOADED, logger=logger, pending=len(links))
        return links

    def _transition_order(
        self,
        *,
        order_id: str,
        signal_id: str,
        asset_id: str,
        timeframe: str,
        action: str,
        from_state: OrderState,
        new_state: OrderState,
        reason: str,
        at: datetime,
    ) -> None:
        self.store.append_order_event(
            PaperOrderEvent(
                ts=at,
                order_id=order_id,
                signal_id=signal_id,
                account_id=self.config.account_id,
                asset_id=asset_id,
                timeframe=timeframe,
                action=action,
                from_state=from_state,
                to_state=new_state,
                reason=reason,
            )
        )
        self._emit(
            EVENT_ORDER_STATE,
            order_id=order_id,
            from_state=from_state.value,
            to_state=new_state.value,
            reason=reason,
        )

    def _expire_unfilled(self, *, asset_id: str, timeframe: str, at: datetime) -> int:
        """Expire ACCEPTED intents that could not fill (deterministic cleanup)."""
        folded = fold_order_events(self.store.load_order_events())
        expired = 0
        for order in sorted(folded.values(), key=lambda o: o.created_at):
            if (
                order.state is not OrderState.ACCEPTED
                or order.asset_id != asset_id
                or order.timeframe != timeframe
            ):
                continue
            self._transition_order(
                order_id=order.order_id,
                signal_id=order.signal_id,
                asset_id=asset_id,
                timeframe=timeframe,
                action=order.action,
                from_state=OrderState.ACCEPTED,
                new_state=OrderState.EXPIRED,
                reason="unfilled (superseded or schedule disabled)",
                at=at,
            )
            record = self.store.load_signal(order.signal_id)
            if record is not None and record.state is SignalState.ACCEPTED:
                record = record.with_transition(new_state=SignalState.EXPIRED, at=at)
                self.store.save_signal(record)
                self.store.append_signal_transition(
                    signal_id=record.signal_id,
                    from_state=SignalState.ACCEPTED.value,
                    to_state=SignalState.EXPIRED.value,
                )
            self._emit(EVENT_PENDING_EXPIRED, signal_id=order.signal_id)
            expired += 1
        return expired

    # -- main cycle ---------------------------------------------------------------

    def run_cycle(
        self, asset_id: str, timeframe: str, *, now: datetime | None = None
    ) -> CycleResult:
        try:
            return self._run_cycle_inner(asset_id, timeframe, now)
        except Exception as exc:  # noqa: BLE001 — a cycle must never crash the loop
            log_event(
                EVENT_CYCLE_FAILED,
                logger=logger,
                account_id=self.config.account_id,
                asset_id=asset_id,
                timeframe=str(timeframe),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return CycleResult(status="cycle_failed", detail=f"{type(exc).__name__}: {exc}")

    def _run_cycle_inner(
        self, asset_id: str, timeframe: str, now: datetime | None
    ) -> CycleResult:
        ts_now = self._utc(now or self._now_fn())
        spec = get_asset(asset_id)
        tf = Timeframe(str(timeframe).lower())
        asset_id = spec.asset_id

        # -- safety guards ------------------------------------------------------
        if not self.config.enabled:
            self._emit(EVENT_TRADING_DISABLED, asset_id=asset_id, timeframe=tf.value)
            return CycleResult(status="trading_disabled")
        state = self.store.load_account()
        if state.halted:
            self._emit(EVENT_EMERGENCY_HALT, reason=state.halt_reason, asset_id=asset_id)
            return CycleResult(status="emergency_halt", detail=state.halt_reason)
        entry = self.config.schedule_for(asset_id, tf.value)
        if entry is None:
            self._expire_unfilled(asset_id=asset_id, timeframe=tf.value, at=ts_now)
            return CycleResult(
                status="schedule_missing",
                detail=f"{asset_id} {tf.value} has no enabled schedule",
            )
        key = ScheduleKey(asset_id, tf.value).value()

        # -- market data ----------------------------------------------------------
        try:
            raw = self.provider.get_ohlcv(spec, tf, limit=BARS_REQUESTED)
        except Exception as exc:  # noqa: BLE001 — vendor taxonomy varies
            log_event(
                EVENT_CYCLE_FAILED,
                logger=logger,
                stage="market_data",
                asset_id=asset_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return CycleResult(status="market_data_failed", detail=str(exc))

        closed = closed_slice(raw, tf_minutes=tf.minutes, now=ts_now)
        if not closed:
            return CycleResult(
                status="no_closed_bar", detail="vendor returned no closed bars"
            )
        decision_bar = closed[-1]
        decision_close = effective_close(decision_bar, tf.minutes)
        self._emit(
            EVENT_MARKET_DATA_UPDATED,
            asset_id=asset_id,
            timeframe=tf.value,
            latest_closed=utc_iso(decision_close),
            closed_bars=len(closed),
        )

        # -- novelty gate ------------------------------------------------------
        sched_state = self.store.load_schedule_state(key) or {}
        last_processed_raw = sched_state.get("last_processed_bar_close")
        if last_processed_raw is not None:
            last_processed = datetime.fromisoformat(last_processed_raw)
            new_bars = [
                bar
                for bar in closed
                if effective_close(bar, tf.minutes) > last_processed
            ]
            if not new_bars:
                self._emit(EVENT_NO_NEW_BAR, asset_id=asset_id, timeframe=tf.value)
                return CycleResult(status="no_new_bar")
        else:
            new_bars = []  # fresh slot: never backfill history

        # -- reconstruct + replay pending intents against the new bars ----------
        portfolio, ledger, position_records, sim = self._reconstruct(asset_id, tf.value)
        links = self._restore_pending(sim)
        replay = self._replay_new_bars(
            sim=sim,
            new_bars=new_bars,
            position_records=position_records,
            links=links,
            now=ts_now,
        )
        if sim.pending is None and links:
            self._expire_unfilled(asset_id=asset_id, timeframe=tf.value, at=ts_now)

        # -- persist accounting exactly once per consumed-bar-set ---------------
        marks = self._mark_prices(position_records, asset_id, decision_bar.close)
        self._finalize_cycle(
            portfolio=portfolio,
            position_records=position_records,
            marks=marks,
            ts_now=ts_now,
            key=key,
            tf=tf,
            offset_minutes=entry.offset_minutes,
            decision_close=decision_close,
        )
        curve = self.store.load_equity_curve()

        # -- content-based duplicate suppression (before any LLM spend) --------
        signal_id = compute_signal_id(
            asset_id=asset_id,
            timeframe=tf.value,
            decision_bar_close=decision_close,
            visible_bars_hash=visible_bars_digest(closed),
            model_ids=self.runner.model_ids,
            config_hash=self.runner.config_hash,
        )
        if self.store.load_signal(signal_id) is not None:
            self._emit(EVENT_DUPLICATE_SUPPRESSED, asset_id=asset_id, signal_id=signal_id)
            return CycleResult(
                status="duplicate_suppressed", signal_id=signal_id, **replay.as_dict()
            )

        # -- research ------------------------------------------------------------
        try:
            signal, no_signal_reason = self.runner.run(asset_id, tf.value)
        except Exception as exc:  # noqa: BLE001 — research failures are data points
            log_event(
                EVENT_CYCLE_FAILED,
                logger=logger,
                stage="research",
                asset_id=asset_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return CycleResult(
                status="research_failed",
                detail=f"{type(exc).__name__}: {exc}",
                **replay.as_dict(),
            )
        if signal is None:
            self._emit(
                EVENT_SIGNAL_NOT_GENERATED, asset_id=asset_id, reason=no_signal_reason
            )
            return CycleResult(
                status="no_signal", detail=no_signal_reason, **replay.as_dict()
            )

        # -- persist generated signal ---------------------------------------------
        record = self._build_record(signal, signal_id, asset_id, tf, decision_close, closed, ts_now)
        self.store.save_signal(record)
        self.store.append_signal_transition(
            signal_id=signal_id,
            from_state="(new)",
            to_state=SignalState.GENERATED.value,
            reason="",
        )
        self._emit(
            EVENT_SIGNAL_GENERATED,
            signal_id=signal_id,
            asset_id=asset_id,
            action=signal.action.value,
            confidence=signal.confidence,
        )

        if signal.action is SignalAction.HOLD:
            return CycleResult(status="hold_no_trade", signal_id=signal_id, **replay.as_dict())

        order_id = f"{signal_id}-E"
        self._transition_order(
            order_id=order_id,
            signal_id=signal_id,
            asset_id=asset_id,
            timeframe=tf.value,
            action=signal.action.value,
            from_state=OrderState.SIGNAL,
            new_state=OrderState.PENDING,
            reason="intent queued",
            at=ts_now,
        )

        # -- validation gate -------------------------------------------------------
        from tradingagents.paper.validator import validate_signal_record

        validation = validate_signal_record(
            record,
            now=ts_now,
            stale_overrides_hours=self.config.stale_overrides_hours,
        )
        if not validation.ok:
            self._reject_signal(
                record=record,
                order_id=order_id,
                from_signal_state=SignalState.GENERATED,
                from_order_state=OrderState.PENDING,
                reason=validation.reason_code,
                detail=validation.detail,
                at=ts_now,
                event=EVENT_VALIDATION_REJECTED,
            )
            return CycleResult(
                status="validation_rejected",
                detail=f"{validation.reason_code}: {validation.detail}",
                signal_id=signal_id,
                order_id=order_id,
                **replay.as_dict(),
            )

        # -- risk engine ---------------------------------------------------------
        equity = portfolio.equity(marks)
        qty_estimate = size_position(
            policy=self.config.sizing,
            equity=max(equity, 1e-9),
            price=signal.entry_reference or decision_bar.close,
        )
        decision = self._risk.evaluate(
            action=signal.action,
            entry_price=signal.entry_reference or decision_bar.close,
            stop_loss=signal.stop_loss_reference,
            quantity=qty_estimate,
            mark_price=decision_bar.close,
            equity=equity,
            day_start_equity=self._day_start_equity(curve, ts_now),
            peak_equity=peak_equity(curve),
            open_positions=len(portfolio.positions),
            gross_exposure=portfolio.gross_exposure(marks),
        )
        if not decision.approved:
            self._reject_signal(
                record=record,
                order_id=order_id,
                from_signal_state=SignalState.GENERATED,
                from_order_state=OrderState.PENDING,
                reason=decision.reason_code,
                detail=decision.detail,
                at=ts_now,
                event=EVENT_RISK_REJECTED,
            )
            return CycleResult(
                status="risk_rejected",
                detail=f"{decision.reason_code}: {decision.detail}",
                signal_id=signal_id,
                order_id=order_id,
                **replay.as_dict(),
            )

        # -- accepted: queued for the NEXT bar's open ------------------------------
        record = record.with_transition(new_state=SignalState.ACCEPTED, at=ts_now)
        self.store.save_signal(record)
        self.store.append_signal_transition(
            signal_id=signal_id,
            from_state=SignalState.GENERATED.value,
            to_state=SignalState.ACCEPTED.value,
            reason="risk approved",
        )
        self._transition_order(
            order_id=order_id,
            signal_id=signal_id,
            asset_id=asset_id,
            timeframe=tf.value,
            action=signal.action.value,
            from_state=OrderState.PENDING,
            new_state=OrderState.ACCEPTED,
            reason="risk approved",
            at=ts_now,
        )
        self._emit(EVENT_SIGNAL_ACCEPTED, signal_id=signal_id, asset_id=asset_id)
        sim.schedule_signal(signal, ts_now)  # restored as pending next cycle

        return CycleResult(
            status="accepted_pending_fill",
            signal_id=signal_id,
            order_id=order_id,
            **replay.as_dict(),
        )

    # -- signal lifecycle helpers ---------------------------------------------

    def _build_record(
        self,
        signal: ResearchSignal,
        signal_id: str,
        asset_id: str,
        tf: Timeframe,
        decision_close: datetime,
        closed_bars: list[Bar],
        ts_now: datetime,
    ) -> PaperSignalRecord:
        snapshot = ResearchSnapshot(
            thesis=signal.thesis,
            key_factors=list(signal.supporting_factors),
            opposing_factors=list(signal.opposing_factors),
            invalidation_conditions=list(signal.invalidation_conditions),
            confidence=signal.confidence,
            models_used=list(signal.models_used),
            generated_at=signal.generated_at,
            market_data_timestamp=closed_bars[-1].timestamp,
            data_sources=list(signal.data_sources),
            research_version=getattr(self.runner, "prompt_version", "unknown"),
            config_hash=self.runner.config_hash,
        )
        return PaperSignalRecord(
            signal_id=signal_id,
            account_id=self.config.account_id,
            environment=self.config.environment,
            asset_id=asset_id,
            timeframe=tf.value,
            state=SignalState.GENERATED,
            decision_bar_close=decision_close,
            generated_at=signal.generated_at,
            market_data_timestamp=closed_bars[-1].timestamp,
            action=signal.action,
            confidence=signal.confidence,
            thesis=signal.thesis,
            supporting_factors=list(signal.supporting_factors),
            opposing_factors=list(signal.opposing_factors),
            invalidation_conditions=list(signal.invalidation_conditions),
            entry_reference=signal.entry_reference,
            stop_loss_reference=signal.stop_loss_reference,
            take_profit_reference=signal.take_profit_reference,
            data_sources=list(signal.data_sources),
            models_used=list(signal.models_used),
            research=snapshot,
            visible_bars_hash=visible_bars_digest(closed_bars),
            updated_at=ts_now,
        )

    def _reject_signal(
        self,
        *,
        record: PaperSignalRecord,
        order_id: str,
        from_signal_state: SignalState,
        from_order_state: OrderState,
        reason: str,
        detail: str,
        at: datetime,
        event: str,
    ) -> None:
        record = record.with_transition(
            new_state=SignalState.REJECTED, reason=reason, at=at
        )
        self.store.save_signal(record)
        self.store.append_signal_transition(
            signal_id=record.signal_id,
            from_state=from_signal_state.value,
            to_state=SignalState.REJECTED.value,
            reason=reason,
        )
        self._transition_order(
            order_id=order_id,
            signal_id=record.signal_id,
            asset_id=record.asset_id,
            timeframe=record.timeframe,
            action=record.action.value,
            from_state=from_order_state,
            new_state=OrderState.REJECTED,
            reason=reason,
            at=at,
        )
        self._emit(event, signal_id=record.signal_id, reason=reason, detail=detail)

    # -- replay -----------------------------------------------------------------

    def _replay_new_bars(
        self,
        *,
        sim: ExecutionSimulator,
        new_bars: list[Bar],
        position_records: dict[str, PositionRecord],
        links: list[tuple[ResearchSignal, str, str]],
        now: datetime,
    ) -> _ReplaySummary:
        """Feed newly closed bars through the simulator; harvest lifecycle."""
        summary = _ReplaySummary()

        def _link_for(pending_signal: ResearchSignal | None) -> tuple[str, str]:
            if pending_signal is None:
                return "", ""
            for obj, sig_id, ord_id in links:
                if obj is pending_signal:
                    return sig_id, ord_id
            return "", ""

        def _save_positions() -> None:
            self.store.save_positions(list(position_records.values()))

        for bar in new_bars:
            rec_before = position_records.get(sim.asset_id)
            n_before = len(sim.ledger.records)
            pend_sig, pend_ord = _link_for(sim.pending.signal if sim.pending else None)

            sim.on_bar_open(bar)

            delta_open = len(sim.ledger.records) - n_before
            if delta_open:  # flip: the previous position exited at this open
                trade = sim.ledger.records[-1]
                summary.closes.append((trade, rec_before, "signal_exit"))
                self._emit(
                    EVENT_POSITION_CLOSED,
                    trade_id=trade.trade_id,
                    asset_id=trade.asset_id,
                    reason="signal_exit",
                    net_pnl=trade.net_pnl,
                    signal_id=rec_before.signal_id if rec_before else "",
                )
                self._mark_order_closed(rec_before, reason="signal_exit", at=now)
                rec_for_stop_check = None
            else:
                rec_for_stop_check = rec_before

            # open detection: position exists and was (re)opened on this bar
            if sim.asset_id in sim.portfolio.positions and (
                rec_before is None or delta_open
            ):
                sim_pos = sim.portfolio.positions[sim.asset_id]
                record = PositionRecord.from_sim_position(
                    sim_pos,
                    account_id=self.config.account_id,
                    signal_id=pend_sig,
                    timeframe=sim.timeframe,
                    position_id=(
                        f"{self.config.account_id}-{sim.asset_id}-"
                        f"{utc_iso(bar.timestamp)}"
                    ),
                    updated_at=now,
                    current_price=bar.close,
                )
                position_records[sim.asset_id] = record
                summary.filled = True
                summary.order_id = pend_ord or None
                self._emit(
                    EVENT_POSITION_OPENED,
                    asset_id=sim.asset_id,
                    direction=record.direction,
                    quantity=record.quantity,
                    entry_price=record.entry_price,
                    signal_id=record.signal_id,
                    order_id=pend_ord or "",
                )
                if pend_ord:
                    folded = fold_order_events(self.store.load_order_events())
                    order = folded.get(pend_ord)
                    if order is not None:
                        self._transition_order(
                            order_id=pend_ord,
                            signal_id=record.signal_id,
                            asset_id=order.asset_id,
                            timeframe=order.timeframe,
                            action=order.action,
                            from_state=OrderState.ACCEPTED,
                            new_state=OrderState.EXECUTED,
                            reason=f"filled at open {utc_iso(effective_close(bar, Timeframe(sim.timeframe).minutes))}",
                            at=now,
                        )
                        self._transition_order(
                            order_id=pend_ord,
                            signal_id=record.signal_id,
                            asset_id=order.asset_id,
                            timeframe=order.timeframe,
                            action=order.action,
                            from_state=OrderState.EXECUTED,
                            new_state=OrderState.OPEN,
                            reason="position live",
                            at=now,
                        )
                    sig_record = self.store.load_signal(record.signal_id)
                    if sig_record is not None and sig_record.state is SignalState.ACCEPTED:
                        sig_record = sig_record.with_transition(
                            new_state=SignalState.EXECUTED, at=now
                        )
                        self.store.save_signal(sig_record)
                        self.store.append_signal_transition(
                            signal_id=record.signal_id,
                            from_state=SignalState.ACCEPTED.value,
                            to_state=SignalState.EXECUTED.value,
                            reason="filled at next bar open",
                        )
                _save_positions()

            # intra-bar stop/target resolution (stop wins ties — Phase 2 rule)
            reason = sim.on_bar_close(bar)
            if reason is not None:
                trade = sim.ledger.records[-1]
                summary.closes.append((trade, rec_for_stop_check, reason))
                if reason == "stop_loss":
                    self._emit(
                        EVENT_STOP_LOSS_TRIGGERED,
                        asset_id=sim.asset_id,
                        trade_id=trade.trade_id,
                    )
                elif reason == "take_profit":
                    self._emit(
                        EVENT_TAKE_PROFIT_TRIGGERED,
                        asset_id=sim.asset_id,
                        trade_id=trade.trade_id,
                    )
                self._emit(
                    EVENT_POSITION_CLOSED,
                    trade_id=trade.trade_id,
                    asset_id=sim.asset_id,
                    reason=reason,
                    net_pnl=trade.net_pnl,
                    signal_id=rec_for_stop_check.signal_id if rec_for_stop_check else "",
                )
                self._mark_order_closed(rec_for_stop_check, reason=reason, at=now)
                position_records.pop(sim.asset_id, None)
                _save_positions()
            elif len(sim.ledger.records) != n_before + delta_open:
                raise RuntimeError(
                    "ledger grew during bar close without a recognised exit reason"
                )

        # persist closed-trade artifacts (trades.jsonl + journals) in order
        for trade, rec_before, reason in summary.closes:
            self._harvest_close(trade, rec_before, reason)
        return summary

    def _mark_order_closed(
        self, rec_before: PositionRecord | None, *, reason: str, at: datetime
    ) -> None:
        """Transition a closed position's order OPEN → CLOSED (if traceable)."""
        if rec_before is None or not rec_before.signal_id:
            return
        order_id = f"{rec_before.signal_id}-E"
        folded = fold_order_events(self.store.load_order_events())
        order = folded.get(order_id)
        if order is None or order.state is not OrderState.OPEN:
            return
        self._transition_order(
            order_id=order_id,
            signal_id=order.signal_id,
            asset_id=order.asset_id,
            timeframe=order.timeframe,
            action=order.action,
            from_state=OrderState.OPEN,
            new_state=OrderState.CLOSED,
            reason=reason,
            at=at,
        )

    def _harvest_close(
        self, trade: TradeRecord, rec_before: PositionRecord | None, reason: str
    ) -> None:
        self.store.append_trade(trade)
        signal_id = rec_before.signal_id if rec_before else ""
        snapshot = None
        if signal_id:
            signal_record = self.store.load_signal(signal_id)
            snapshot = signal_record.research if signal_record else None
        entry = JournalEntry(
            trade_id=trade.trade_id,
            signal_id=signal_id,
            account_id=self.config.account_id,
            asset_id=trade.asset_id,
            timeframe=trade.timeframe,
            direction=trade.direction,
            opened_at=trade.entry_timestamp,
            closed_at=trade.exit_timestamp,
            exit_reason=reason,
            snapshot=snapshot
            or ResearchSnapshot(
                thesis="unattributed close (no signal record found)",
                confidence=0.0,
                generated_at=trade.signal_generated_at,
                research_version="unknown",
            ),
            trade_summary={
                "net_pnl": trade.net_pnl,
                "gross_pnl": trade.gross_pnl,
                "transaction_costs": trade.transaction_costs,
                "return_pct": trade.return_pct,
                "exit_price": trade.exit_price,
                "bars_held": trade.bars_held,
            },
        )
        self.store.save_journal(entry)

    # -- finalization -------------------------------------------------------------

    def _finalize_cycle(
        self,
        *,
        portfolio: Portfolio,
        position_records: dict[str, PositionRecord],
        marks: dict[str, float],
        ts_now: datetime,
        key: str,
        tf: Timeframe,
        offset_minutes: int,
        decision_close: datetime,
    ) -> None:
        for aid, rec in position_records.items():
            if aid in marks:
                rec.current_price = marks[aid]
                rec.updated_at = ts_now
        self.store.save_positions(list(position_records.values()))

        state = self.store.load_account()
        state.cash = portfolio.cash
        state.realized_pnl = portfolio.realized_pnl
        state.total_costs_paid = portfolio.total_costs_paid
        state.closed_trades = portfolio.closed_trades
        state.updated_at = ts_now
        self.store.save_account(state)

        equity = portfolio.equity(marks)
        unrealized = portfolio.unrealized_pnl(marks)
        prior_curve = self.store.load_equity_curve()
        peak = peak_equity(prior_curve) or state.initial_capital
        drawdown = max(0.0, (1 - equity / peak) * 100) if peak > 0 else 0.0
        self.store.append_equity(
            EquitySnapshot(
                timestamp=ts_now,
                equity=equity,
                cash=portfolio.cash,
                exposure=portfolio.gross_exposure(marks),
                open_positions=len(portfolio.positions),
                drawdown_pct=drawdown,
                balance=portfolio.cash,
                realized_pnl=portfolio.realized_pnl,
                unrealized_pnl=unrealized,
            )
        )
        self._emit(
            EVENT_EQUITY_SNAPSHOT,
            equity=equity,
            drawdown_pct=drawdown,
            open_positions=len(portfolio.positions),
        )

        self._append_daily_rollup(ts_now)
        next_run = next_run_after(
            last_processed_bar_close=decision_close,
            tf_minutes=tf.minutes,
            offset_minutes=offset_minutes,
        )
        self.store.save_schedule_state(
            key,
            {
                "last_run_at": utc_iso(ts_now),
                "last_processed_bar_close": utc_iso(decision_close),
                "next_run_at": utc_iso(next_run),
            },
        )

    def _append_daily_rollup(self, ts_now: datetime) -> None:
        date_str = ts_now.date().isoformat()
        curve = self.store.load_equity_curve()
        day_points = [
            p for p in curve if p.timestamp.date().isoformat() == date_str
        ]
        day_trades = [
            t
            for t in self.store.load_trades()
            if t.exit_timestamp.date().isoformat() == date_str
        ]
        previous = [p for p in curve if p.timestamp.date().isoformat() != date_str]
        row = build_daily_row(
            date_str=date_str,
            day_curve=day_points,
            day_trades=day_trades,
            previous_end_equity=previous[-1].equity if previous else None,
        )
        if row is not None:
            self.store.append_daily(row)

    # -- misc ------------------------------------------------------------------

    def _mark_prices(
        self,
        position_records: dict[str, PositionRecord],
        asset_id: str,
        this_asset_mark: float,
    ) -> dict[str, float]:
        marks: dict[str, float] = {}
        for aid, rec in position_records.items():
            if aid == asset_id:
                marks[aid] = this_asset_mark
            elif rec.current_price is not None:
                marks[aid] = rec.current_price
        return marks

    def _day_start_equity(
        self, curve: list[EquitySnapshot], now: datetime
    ) -> float | None:
        boundary = self._utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
        for point in curve:
            if point.timestamp >= boundary:
                return point.equity
        return None

    # -- dashboard-facing read APIs (Phase 4 consumes these unchanged) ----------

    def account_summary(self) -> dict[str, Any]:
        from tradingagents.paper.performance import live_performance_stats

        state = self.store.load_account()
        positions = self.store.load_positions()
        marks = {
            rec.asset_id: rec.current_price
            for rec in positions
            if rec.current_price is not None
        }
        portfolio = Portfolio(initial_capital=state.initial_capital)
        portfolio.cash = state.cash
        portfolio.realized_pnl = state.realized_pnl
        portfolio.total_costs_paid = state.total_costs_paid
        portfolio.closed_trades = state.closed_trades
        for rec in positions:
            portfolio.open_position(rec.to_sim_position())
        equity = portfolio.equity(marks) if marks else state.cash
        unrealized = portfolio.unrealized_pnl(marks) if marks else 0.0
        curve = self.store.load_equity_curve()
        stats = live_performance_stats(
            records=self.store.load_trades(), equity_curve=curve
        )
        orders = fold_order_events(self.store.load_order_events())
        return {
            "state": state,
            "positions": positions,
            "equity": equity,
            "unrealized_pnl": unrealized,
            "stats": stats,
            "daily": self.store.load_daily_folded(),
            "open_orders": [o for o in orders.values() if o.state is OrderState.OPEN],
            "orders_total": len(orders),
        }

    def build_report(self) -> Any:
        summary = self.account_summary()
        return build_account_report(
            state=summary["state"],
            positions=summary["positions"],
            equity=summary["equity"],
            unrealized_pnl=summary["unrealized_pnl"],
            stats=summary["stats"],
            daily=summary["daily"],
            now=self._utc(self._now_fn()),
        )


class _ReplaySummary:
    """Accumulator for one replay pass."""

    def __init__(self) -> None:
        self.filled = False
        self.order_id: str | None = None
        self.closes: list[tuple[TradeRecord, PositionRecord | None, str]] = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "filled": self.filled,
            "closed_trades": len(self.closes),
            "trade_ids": [trade.trade_id for trade, _, _ in self.closes],
        }


__all__ = [
    "ClosedBarMarketDataProvider",
    "CycleResult",
    "LiveResearchRunner",
    "PaperTradingEngine",
    "closed_slice",
    "effective_close",
]
