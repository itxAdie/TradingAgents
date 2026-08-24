"""Restart safety: pending-intent restore, idempotency, downtime replay.

These tests simulate process restarts by building *fresh engine instances*
over the same store — exactly what happens when the CLI loop restarts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.paper_helpers import (
    T0,
    Clock,
    make_engine,
    make_signal,
    scenario_bars,
)
from tradingagents.paper.models import OrderState, fold_order_events
from tradingagents.research.schemas import SignalAction

DECISION1_AT = datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)
FILL_AT = datetime(2026, 8, 24, 14, 5, tzinfo=timezone.utc)


def _fresh(store_root: Path, bars_count: int, clock_at: datetime):
    clock = Clock(clock_at)
    engine, runner = make_engine(
        store_root=store_root, bars=scenario_bars()[:bars_count], clock=clock
    )
    return engine, runner, clock


def _accept_first_signal(store_root: Path):
    """Cycle 1 on B3; returns (engine, runner, clock) after acceptance."""
    engine, runner, clock = _fresh(store_root, 4, DECISION1_AT)
    engine.init_account()
    runner.canned_signal = make_signal(generated_at=DECISION1_AT)
    result = engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT)
    assert result.status == "accepted_pending_fill"
    return engine, runner, clock, result.signal_id


class TestPendingRestoreAcrossRestart:
    def test_intent_fills_exactly_once_after_restart(self, tmp_path: Path) -> None:
        _accept_first_signal(tmp_path)

        # "restart": brand-new engine instance over the same store, B4 arrives
        engine2, runner2, clock2 = _fresh(tmp_path, 5, FILL_AT)
        runner2.canned_signal = make_signal(
            action=SignalAction.HOLD, generated_at=FILL_AT
        )
        r = engine2.run_cycle("XAUUSD", "1h", now=FILL_AT)
        assert r.filled is True

        positions = engine2.store.load_positions()
        assert len(positions) == 1  # restored intent filled exactly once
        orders = fold_order_events(engine2.store.load_order_events())
        states = [o.state for o in orders.values()]
        assert states.count(OrderState.OPEN) == 1
        assert OrderState.ACCEPTED not in states  # no duplicate pending left

    def test_no_new_bar_before_fill_keeps_pending_intact(self, tmp_path: Path) -> None:
        _accept_first_signal(tmp_path)

        # restart while still inside the same bar window: nothing to do yet
        engine2, _, _ = _fresh(
            tmp_path, 4, DECISION1_AT + timedelta(minutes=10)
        )
        result = engine2.run_cycle("XAUUSD", "1h")
        assert result.status == "no_new_bar"

        # and the accepted intent is still armed for the next bar's open
        folded = fold_order_events(engine2.store.load_order_events())
        assert any(o.state is OrderState.ACCEPTED for o in folded.values())


class TestIdempotency:
    def test_replayed_slot_cannot_double_execute(self, tmp_path: Path) -> None:
        """Crash-replay of an already-consumed slot must be a no-op.

        Simulates bookkeeping rewind (crash between fill and scheduler
        advance): the same decision-bar signal id is regenerated, the store
        already holds it, so the cycle suppresses before any research spend.
        """
        engine, runner, clock, sig_id = _accept_first_signal(tmp_path)

        # fill on B4 normally
        clock.now = FILL_AT
        engine.provider.bars.append(scenario_bars()[4])
        runner.canned_signal = make_signal(action=SignalAction.HOLD, generated_at=FILL_AT)
        assert engine.run_cycle("XAUUSD", "1h", now=FILL_AT).filled is True

        # rewind scheduler bookkeeping to simulate a crash right after the fill:
        # the store still thinks only B3 (eff. close 13:00) was consumed
        key = "XAUUSD:1h"
        state = engine.store.load_schedule_state(key)
        engine.store.save_schedule_state(
            key,
            {
                **state,
                "last_processed_bar_close": (T0 + timedelta(hours=4)).isoformat(),
            },
        )

        # full restart; same data re-delivered
        engine2, runner2, _ = _fresh(tmp_path, 5, FILL_AT)
        calls_before = runner2.calls
        r2 = engine2.run_cycle("XAUUSD", "1h", now=FILL_AT)
        assert r2.status == "duplicate_suppressed"
        assert r2.signal_id is not None
        # dedupe happens BEFORE research — zero additional LLM spend
        assert runner2.calls == calls_before
        assert len(engine2.store.load_positions()) == 1
        assert len(engine2.store.load_trades()) == 0


class TestDowntimeResolution:
    def test_stop_hit_while_offline_resolves_on_real_missed_bar(self, tmp_path: Path) -> None:
        engine, runner, clock, sig_id = _accept_first_signal(tmp_path)

        # fill at B4 open
        clock.now = FILL_AT
        engine.provider.bars.append(scenario_bars()[4])
        runner.canned_signal = make_signal(action=SignalAction.HOLD, generated_at=FILL_AT)
        assert engine.run_cycle("XAUUSD", "1h", now=FILL_AT).filled is True

        # process down for TWO bars (B5 crash + B6); one cycle catches up
        back_online = datetime(2026, 8, 24, 16, 5, tzinfo=timezone.utc)
        engine2, runner2, _ = _fresh(tmp_path, 7, back_online)
        runner2.canned_signal = make_signal(
            action=SignalAction.HOLD, generated_at=back_online
        )
        r = engine2.run_cycle("XAUUSD", "1h", now=back_online)
        assert r.status == "hold_no_trade"
        assert r.closed_trades == 1

        trades = engine2.store.load_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "stop_loss"
        # exit resolved on the actual historical bar that breached the stop
        b5 = scenario_bars()[5]
        assert trade.exit_timestamp == b5.timestamp
        assert trade.raw_exit_price == 2015.0 * 0.97

        # scheduler advanced across BOTH consumed bars
        sched = engine2.store.load_schedule_state("XAUUSD:1h")
        from tests.paper_helpers import T0

        assert sched["last_processed_bar_close"] == (
            T0 + timedelta(hours=7)
        ).isoformat()  # B6 effective close 16:00... see below


class TestKillSwitchResume:
    def test_halt_blocks_then_resume_allows(self, tmp_path: Path) -> None:
        engine, runner, clock, _ = _accept_first_signal(tmp_path)
        engine.store.set_halted(True, "halted mid-flight")

        halted = engine.run_cycle("XAUUSD", "1h")
        assert halted.status == "emergency_halt"

        engine.store.set_halted(False)
        # resume: pending intent still restores and fills on the next bar
        clock.now = FILL_AT
        engine.provider.bars.append(scenario_bars()[4])
        runner.canned_signal = make_signal(action=SignalAction.HOLD, generated_at=FILL_AT)
        r = engine.run_cycle("XAUUSD", "1h", now=FILL_AT)
        assert r.filled is True
