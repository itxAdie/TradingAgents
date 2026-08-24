"""Lifecycle state machines and persisted record bridges (paper/models.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.paper.models import (
    AccountState,
    DailyPerformanceRow,
    EquitySnapshot,
    JournalEntry,
    JournalNote,
    OrderState,
    PaperOrderEvent,
    PaperSignalRecord,
    PositionRecord,
    ResearchSnapshot,
    SignalState,
    fold_order_events,
    order_can_transition,
    signal_can_transition,
)

NOW = datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)


def _event(
    order_id: str = "o1",
    *,
    from_state: OrderState = OrderState.SIGNAL,
    to_state: OrderState = OrderState.PENDING,
    ts: datetime = NOW,
) -> PaperOrderEvent:
    return PaperOrderEvent(
        ts=ts,
        order_id=order_id,
        signal_id="sig1",
        account_id="a",
        asset_id="XAUUSD",
        timeframe="1h",
        action="BUY",
        from_state=from_state,
        to_state=to_state,
        reason="test",
    )


class TestOrderTransitions:
    def test_happy_path(self) -> None:
        path = [
            OrderState.PENDING,
            OrderState.ACCEPTED,
            OrderState.EXECUTED,
            OrderState.OPEN,
            OrderState.CLOSED,
        ]
        current = OrderState.SIGNAL
        for nxt in path:
            assert order_can_transition(current, nxt)
            current = nxt
        assert not order_can_transition(OrderState.CLOSED, OrderState.OPEN)

    def test_rejection_and_expiry_paths_legal(self) -> None:
        assert order_can_transition(OrderState.SIGNAL, OrderState.REJECTED)
        assert order_can_transition(OrderState.PENDING, OrderState.REJECTED)
        assert order_can_transition(OrderState.ACCEPTED, OrderState.EXPIRED)
        assert not order_can_transition(OrderState.OPEN, OrderState.REJECTED)
        assert not order_can_transition(OrderState.CLOSED, OrderState.FAILED)


class TestFoldOrderEvents:
    def test_fold_reproduces_full_lifecycle(self) -> None:
        events = [
            _event(to_state=OrderState.PENDING),
            _event(from_state=OrderState.PENDING, to_state=OrderState.ACCEPTED),
            _event(from_state=OrderState.ACCEPTED, to_state=OrderState.EXECUTED),
            _event(from_state=OrderState.EXECUTED, to_state=OrderState.OPEN),
            _event(from_state=OrderState.OPEN, to_state=OrderState.CLOSED),
        ]
        orders = fold_order_events(events)
        assert orders["o1"].state is OrderState.CLOSED
        assert orders["o1"].reason == "test"

    def test_fold_requires_signal_start(self) -> None:
        with pytest.raises(ValueError, match="must start from 'signal'"):
            fold_order_events([_event(from_state=OrderState.PENDING)])

    def test_fold_rejects_illegal_jump(self) -> None:
        with pytest.raises(ValueError, match="illegal order transition"):
            fold_order_events([_event(to_state=OrderState.CLOSED)])

    def test_multiple_orders_stay_separate(self) -> None:
        events = [
            _event("o1"),
            _event("o2"),
            _event("o2", from_state=OrderState.PENDING, to_state=OrderState.REJECTED),
        ]
        folded = fold_order_events(events)
        assert folded["o1"].state is OrderState.PENDING
        assert folded["o2"].state is OrderState.REJECTED


class TestSignalTransitions:
    def test_table(self) -> None:
        assert signal_can_transition(SignalState.GENERATED, SignalState.ACCEPTED)
        assert signal_can_transition(SignalState.GENERATED, SignalState.REJECTED)
        assert signal_can_transition(SignalState.GENERATED, SignalState.SUPERSEDED)
        assert signal_can_transition(SignalState.ACCEPTED, SignalState.EXECUTED)
        assert signal_can_transition(SignalState.ACCEPTED, SignalState.EXPIRED)
        assert not signal_can_transition(SignalState.EXECUTED, SignalState.REJECTED)
        assert not signal_can_transition(SignalState.REJECTED, SignalState.ACCEPTED)


def _record(state: SignalState = SignalState.GENERATED) -> PaperSignalRecord:
    return PaperSignalRecord(
        signal_id="sig1",
        account_id="a",
        environment="test",
        asset_id="XAUUSD",
        timeframe="1h",
        state=state,
        decision_bar_close=NOW - timedelta(hours=1),
        generated_at=NOW - timedelta(minutes=55),
        action="BUY",
        confidence=0.7,
        thesis="t",
        entry_reference=2015.0,
        stop_loss_reference=1954.55,
        take_profit_reference=2135.9,
        research=ResearchSnapshot(
            thesis="t", confidence=0.7, generated_at=NOW, research_version="v1"
        ),
        updated_at=NOW,
    )


class TestPaperSignalRecord:
    def test_with_transition_records_rejection_reason(self) -> None:
        rejected = _record().with_transition(
            new_state=SignalState.REJECTED, reason="risk_per_trade", at=NOW
        )
        assert rejected.state is SignalState.REJECTED
        assert rejected.rejection_reason == "risk_per_trade"

    def test_illegal_transition_raises(self) -> None:
        with pytest.raises(ValueError, match="illegal signal transition"):
            _record(SignalState.REJECTED).with_transition(
                new_state=SignalState.ACCEPTED, at=NOW
            )

    def test_to_research_signal_roundtrip(self) -> None:
        rebuilt = _record().to_research_signal()
        assert rebuilt.action.value == "BUY"
        assert rebuilt.entry_reference == 2015.0
        assert rebuilt.stop_loss_reference == 1954.55
        # confidence 0.7 >= 0.6 -> LOW risk level mapping
        assert rebuilt.risk_level.value == "LOW"


class TestPositionRecordBridge:
    def test_roundtrip_through_sim_position(self) -> None:
        rec = PositionRecord(
            position_id="p1",
            account_id="a",
            signal_id="sig1",
            asset_id="XAUUSD",
            timeframe="1h",
            direction=1,
            quantity=0.5,
            entry_price=2000.0,
            raw_entry_price=1999.5,
            entry_time=NOW,
            updated_at=NOW,
            stop_loss=1940.0,
            take_profit=2100.0,
            current_price=2020.0,
        )
        sim = rec.to_sim_position()
        assert sim.direction == 1 and sim.quantity == 0.5
        back = PositionRecord.from_sim_position(
            sim,
            account_id="a",
            signal_id="sig1",
            timeframe="1h",
            position_id="p1",
            updated_at=NOW,
            current_price=2020.0,
        )
        assert back == rec
        assert back.unrealized_pnl == pytest.approx(10.0)

    def test_unrealized_pnl_none_without_mark(self) -> None:
        rec = PositionRecord(
            position_id="p1",
            account_id="a",
            signal_id="sig1",
            asset_id="XAUUSD",
            timeframe="1h",
            direction=-1,
            quantity=1.0,
            entry_price=100.0,
            entry_time=NOW,
            updated_at=NOW,
        )
        assert rec.unrealized_pnl is None


class TestEquitySnapshotAndDaily:
    def test_snapshot_is_an_equity_point(self) -> None:
        snap = EquitySnapshot(
            timestamp=NOW,
            equity=10_000.0,
            cash=10_000.0,
            exposure=0.0,
            open_positions=0,
            drawdown_pct=0.0,
            balance=10_000.0,
        )
        # compute_stats consumes EquityPoint lists; subclass keeps compat.
        from tradingagents.backtest.analytics import EquityPoint

        assert isinstance(snap, EquityPoint)

    def test_daily_row_fields(self) -> None:
        row = DailyPerformanceRow(
            date="2026-08-24",
            starting_equity=100.0,
            ending_equity=101.0,
            daily_return_pct=1.0,
        )
        assert row.trades_closed == 0


class TestJournal:
    def test_note_appended(self) -> None:
        entry = JournalEntry(
            trade_id="t1",
            signal_id="s1",
            account_id="a",
            asset_id="XAUUSD",
            timeframe="1h",
            direction=1,
            opened_at=NOW,
            snapshot=ResearchSnapshot(
                thesis="t", confidence=0.7, generated_at=NOW, research_version="v1"
            ),
            notes=[JournalNote(timestamp=NOW, text="first")],
        )
        assert entry.notes[0].text == "first"

    def test_empty_note_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            JournalNote(timestamp=NOW, text="")


class TestAccountState:
    def test_defaults_safe(self) -> None:
        state = AccountState(
            account_id="a", environment="test", initial_capital=100.0, cash=100.0
        )
        assert state.halted is False
        assert state.schema_version == 1
