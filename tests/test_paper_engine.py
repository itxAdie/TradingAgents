"""Full paper-trading cycles against scripted data + canned research.

Offline and deterministic: SIMULATED-status provider, fake runner, injected
clock. Covers the P3.0 pipeline: guards → fetch → novelty → replay →
accounting → dedupe → research → validate → risk → accept.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.paper_helpers import (
    Clock,
    default_config,
    make_engine,
    make_signal,
    scenario_bars,
)
from tradingagents.paper.config import PaperRiskLimits
from tradingagents.paper.models import OrderState, SignalState, fold_order_events
from tradingagents.research.schemas import SignalAction

DECISION1_AT = datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)  # after B3 closes
FILL_AT = datetime(2026, 8, 24, 14, 5, tzinfo=timezone.utc)  # after B4 closes
STOP_AT = datetime(2026, 8, 24, 15, 5, tzinfo=timezone.utc)  # after B5 closes

# ExecutionConfig defaults: 1 slippage + 1 half-spread + 0.5 commission = 2.5 bps
COST_FACTOR = 1 + 2.5 / 10_000


def _engine_at(
    store_root: Path,
    *,
    bars_count: int = 4,
    clock_at: datetime = DECISION1_AT,
    config=None,
):
    clock = Clock(clock_at)
    engine, runner = make_engine(
        store_root=store_root,
        bars=scenario_bars()[:bars_count],
        clock=clock,
        config=config,
    )
    return engine, runner, clock


class TestGuards:
    def test_kill_switch_blocks_before_anything_else(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path, config=default_config(enabled=False))
        result = engine.run_cycle("XAUUSD", "1h")
        assert result.status == "trading_disabled"
        assert runner.calls == 0

    def test_emergency_halt_persisted_flag(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path)
        engine.init_account()
        engine.store.set_halted(True, "operator pressed the button")
        result = engine.run_cycle("XAUUSD", "1h")
        assert result.status == "emergency_halt"
        assert "operator" in result.detail
        assert runner.calls == 0

    def test_schedule_missing(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path)
        engine.init_account()
        result = engine.run_cycle("BTCUSD", "1h")
        assert result.status == "schedule_missing"
        assert runner.calls == 0

    def test_market_data_failure_fails_loudly_not_silently(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path)
        engine.init_account()
        engine.provider.fail_with = RuntimeError("vendor down")
        result = engine.run_cycle("XAUUSD", "1h")
        assert result.status == "market_data_failed"
        assert "vendor down" in result.detail
        assert runner.calls == 0  # no LLM spend without data

    def test_research_failure_is_a_data_point(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path)
        engine.init_account()
        runner.fail_with = RuntimeError("llm timeout")
        result = engine.run_cycle("XAUUSD", "1h")
        assert result.status == "research_failed"
        # accounting still advanced honestly for the consumed slot
        curve = engine.store.load_equity_curve()
        assert len(curve) == 2  # anchor + cycle snapshot


class TestFullBuyLifecycle:
    def test_accept_then_fill_then_stop_out(self, tmp_path) -> None:
        engine, runner, clock = _engine_at(tmp_path)
        state = engine.init_account()
        assert state.cash == 10_000.0

        # -- cycle 1: decision on B3 (12:00 bar), accepted, fills next bar --
        runner.canned_signal = make_signal(generated_at=DECISION1_AT)
        r1 = engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT)
        assert r1.status == "accepted_pending_fill"
        assert r1.filled is False
        sig_id_1 = r1.signal_id
        assert sig_id_1 is not None

        record = engine.store.load_signal(sig_id_1)
        assert record.state is SignalState.ACCEPTED
        orders = fold_order_events(engine.store.load_order_events())
        assert orders[f"{sig_id_1}-E"].state is OrderState.ACCEPTED

        # nothing filled yet; cash untouched (settled-cash model)
        assert engine.store.load_positions() == []

        # -- cycle 2: B4 arrives; pending intent fills at B4's open --
        clock.now = FILL_AT
        engine.provider.bars.append(scenario_bars()[4])
        runner.canned_signal = make_signal(
            action=SignalAction.HOLD, generated_at=FILL_AT
        )
        r2 = engine.run_cycle("XAUUSD", "1h", now=FILL_AT)
        assert r2.status == "hold_no_trade"
        assert r2.filled is True

        positions = engine.store.load_positions()
        assert len(positions) == 1
        pos = positions[0]
        expected_entry = 2016.0 * COST_FACTOR
        assert pos.entry_price == pytest.approx(expected_entry)
        assert pos.direction == 1
        assert pos.signal_id == sig_id_1
        assert pos.quantity == pytest.approx(1_000.0 / 2016.0)
        assert pos.stop_loss == pytest.approx(2015.0 * 0.97)
        assert pos.take_profit == pytest.approx(2015.0 * 1.06)

        orders = fold_order_events(engine.store.load_order_events())
        assert orders[f"{sig_id_1}-E"].state is OrderState.OPEN
        assert engine.store.load_signal(sig_id_1).state is SignalState.EXECUTED

        # unrealized profit at mark 2020; settled cash unchanged
        qty = pos.quantity
        summary = engine.account_summary()
        assert summary["state"].cash == pytest.approx(10_000.0)
        assert summary["unrealized_pnl"] == pytest.approx(
            (2020.0 - expected_entry) * qty, abs=1e-9
        )

        # -- cycle 3: B5 crashes through the stop; position closed honestly --
        clock.now = STOP_AT
        engine.provider.bars.append(scenario_bars()[5])
        runner.canned_signal = make_signal(
            action=SignalAction.HOLD, generated_at=STOP_AT
        )
        r3 = engine.run_cycle("XAUUSD", "1h", now=STOP_AT)
        assert r3.status == "hold_no_trade"
        assert r3.closed_trades == 1
        assert len(r3.trade_ids) == 1

        trades = engine.store.load_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "stop_loss"
        # honest fill: resolved at the real historical bar's stop level
        assert trade.raw_exit_price == pytest.approx(2015.0 * 0.97)
        assert trade.exit_timestamp == scenario_bars()[5].timestamp
        assert trade.net_pnl < 0
        assert -35.0 < trade.net_pnl < -28.0

        # order OPEN -> CLOSED; positions cleared; cash settled
        orders = fold_order_events(engine.store.load_order_events())
        assert orders[f"{sig_id_1}-E"].state is OrderState.CLOSED
        assert engine.store.load_positions() == []
        state_after = engine.store.load_account()
        assert state_after.cash == pytest.approx(10_000.0 + trade.net_pnl)
        assert state_after.realized_pnl == pytest.approx(trade.net_pnl)
        assert state_after.closed_trades == 1

        # journal carries the exact research snapshot of the entry decision
        journal = engine.store.load_journal(trade.trade_id)
        assert journal is not None
        assert journal.snapshot.thesis == "deterministic test thesis"
        assert journal.exit_reason == "stop_loss"

    def test_equity_curve_and_daily_rollup_grow_per_cycle(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path)
        engine.init_account()
        runner.canned_signal = make_signal(generated_at=DECISION1_AT)
        engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT)
        curve = engine.store.load_equity_curve()
        assert len(curve) == 2
        assert curve[0].equity == 10_000.0  # capital anchor point
        daily = engine.store.load_daily_folded()
        assert len(daily) == 1
        assert daily[0].date == "2026-08-24"


class TestRejectionPaths:
    def test_invalid_levels_rejected_by_validator(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path)
        engine.init_account()
        # BUY with stop ABOVE entry — logically invalid
        runner.canned_signal = make_signal(
            generated_at=DECISION1_AT, price=2015.0, stop_distance_pct=-0.03
        )
        result = engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT)
        assert result.status == "validation_rejected"
        assert "invalid_levels" in result.detail
        record = engine.store.load_signal(result.signal_id)
        assert record.state is SignalState.REJECTED
        orders = fold_order_events(engine.store.load_order_events())
        assert orders[result.order_id].state is OrderState.REJECTED
        assert engine.store.load_trades() == []

    def test_risk_veto_blocks_entry_but_cycle_succeeds(self, tmp_path) -> None:
        config = default_config(
            risk=PaperRiskLimits(max_risk_per_trade_pct=0.0001)
        )
        engine, runner, _ = _engine_at(tmp_path, config=config)
        engine.init_account()
        runner.canned_signal = make_signal(generated_at=DECISION1_AT)
        result = engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT)
        assert result.status == "risk_rejected"
        assert result.detail.startswith("risk_per_trade")
        assert engine.store.load_signal(result.signal_id).state is SignalState.REJECTED
        assert engine.store.load_positions() == []


class TestNoSignal:
    def test_runner_returns_none(self, tmp_path) -> None:
        engine, runner, _ = _engine_at(tmp_path)
        engine.init_account()
        runner.canned_signal = None
        runner.no_signal_reason = "insufficient confluence"
        result = engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT)
        assert result.status == "no_signal"
        assert "insufficient confluence" in result.detail


class TestNoveltyGate:
    def test_no_new_bar_skips_without_llm_spend(self, tmp_path) -> None:
        engine, runner, clock = _engine_at(tmp_path)
        engine.init_account()
        runner.canned_signal = make_signal(generated_at=DECISION1_AT)
        first = engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT)
        assert first.status == "accepted_pending_fill"
        calls_after_first = runner.calls

        # same clock, no new bars: novelty gate blocks before research
        repeat = engine.run_cycle("XAUUSD", "1h", now=DECISION1_AT + timedelta(minutes=10))
        assert repeat.status == "no_new_bar"
        assert runner.calls == calls_after_first
