"""Risk engine: ordered deterministic vetoes — the AI can never bypass these."""

from __future__ import annotations

import pytest

from tradingagents.paper.config import PaperRiskLimits
from tradingagents.paper.risk import RiskEngine
from tradingagents.research.schemas import SignalAction


def make_engine(**limit_overrides) -> RiskEngine:
    return RiskEngine(
        limits=PaperRiskLimits(**limit_overrides),
        kill_switch_enabled=True,
        is_halted=lambda: (False, ""),
    )


def evaluate(engine: RiskEngine = None, **overrides):
    kwargs = {
        "action": SignalAction.BUY,
        "entry_price": 100.0,
        "stop_loss": 99.0,
        "quantity": 1.0,
        "mark_price": 100.0,
        "equity": 10_000.0,
        "day_start_equity": 10_000.0,
        "peak_equity": 10_000.0,
        "open_positions": 0,
        "gross_exposure": 0.0,
    }
    kwargs.update(overrides)
    return (engine or make_engine()).evaluate(**kwargs)


class TestApprovals:
    def test_baseline_buy_approved(self) -> None:
        decision = evaluate()
        assert decision.approved

    def test_hold_always_approvable(self) -> None:
        decision = evaluate(action=SignalAction.HOLD, stop_loss=None, entry_price=0.0)
        assert decision.approved


class TestGuardVetoes:
    def test_kill_switch_blocks_first(self) -> None:
        engine = RiskEngine(
            limits=PaperRiskLimits(),
            kill_switch_enabled=False,
            is_halted=lambda: (False, ""),
        )
        d = evaluate(engine)
        assert (d.approved, d.reason_code) == (False, "trading_disabled")

    def test_halt_flag_blocks(self) -> None:
        engine = RiskEngine(
            limits=PaperRiskLimits(),
            kill_switch_enabled=True,
            is_halted=lambda: (True, "operator halt"),
        )
        d = evaluate(engine)
        assert (d.approved, d.reason_code) == (False, "emergency_halt")
        assert "operator halt" in d.detail


class TestAccountLevelVetoes:
    def test_daily_loss_limit(self) -> None:
        d = evaluate(day_start_equity=10_350.0, equity=10_000.0)  # -3.38%
        assert (d.approved, d.reason_code) == (False, "daily_loss_limit")

    def test_small_daily_loss_still_approved(self) -> None:
        d = evaluate(day_start_equity=10_200.0, equity=10_000.0)  # -1.96%
        assert d.approved

    def test_daily_loss_exactly_at_limit_vetoes(self) -> None:
        d = evaluate(
            day_start_equity=10_000.0, equity=9_700.0
        )  # exactly -3% default
        assert (d.approved, d.reason_code) == (False, "daily_loss_limit")

    def test_drawdown_veto_after_daily_passes(self) -> None:
        # daily loss small (-1%), but peak far above -> drawdown ~10% >= 5%
        tight = make_engine(max_drawdown_pct=0.05)
        d = evaluate(
            tight,
            day_start_equity=10_101.0,
            equity=10_000.0,
            peak_equity=11_111.12,
        )
        assert (d.approved, d.reason_code) == (False, "max_drawdown")

    def test_ordering_daily_loss_beats_max_positions(self) -> None:
        d = evaluate(
            day_start_equity=20_000.0,  # -50% daily -> daily_loss first
            equity=10_000.0,
            open_positions=99,
        )
        assert d.reason_code == "daily_loss_limit"


class TestPositionVetoes:
    def test_max_positions(self) -> None:
        d = evaluate(open_positions=1)  # default max_open_positions=1
        assert (d.approved, d.reason_code) == (False, "max_positions")

    def test_missing_stop_rejected_for_buy(self) -> None:
        d = evaluate(stop_loss=None)
        assert (d.approved, d.reason_code) == (False, "missing_stop_level")

    def test_risk_per_trade_budget(self) -> None:
        # qty 1 * |100-95| = 5 => 5/10_000 = 0.05% <= 1%: approved
        assert evaluate(entry_price=100.0, stop_loss=95.0).approved
        # risk budget tightened to 0.04% -> same trade vetoed
        tight = make_engine(max_risk_per_trade_pct=0.0004)
        d = evaluate(tight, entry_price=100.0, stop_loss=95.0)
        assert (d.approved, d.reason_code) == (False, "risk_per_trade")

    def test_projected_exposure_cap(self) -> None:
        d = evaluate(quantity=60.0, gross_exposure=0.0)  # 6000/10000 = 60%... ok at 100
        assert d.approved
        capped = make_engine(max_total_exposure_pct=50.0)
        d2 = evaluate(capped, quantity=60.0, gross_exposure=0.0)
        assert (d2.approved, d2.reason_code) == (False, "max_exposure")

    def test_existing_positions_count_toward_exposure(self) -> None:
        d = evaluate(gross_exposure=9_500.0, quantity=1.0)  # projected 96% <= 100
        assert d.approved
        near = make_engine(max_total_exposure_pct=90.0)
        d2 = evaluate(near, gross_exposure=9_500.0, quantity=1.0)
        assert (d2.approved, d2.reason_code) == (False, "max_exposure")

    def test_single_position_notional_cap(self) -> None:
        capped = make_engine(max_position_notional=500.0)
        d = evaluate(capped, quantity=6.0)  # 600 notional > 500 cap, exposure tiny
        assert (d.approved, d.reason_code) == (False, "position_notional_cap")

    def test_insufficient_equity(self) -> None:
        d = evaluate(equity=0.0, day_start_equity=None, peak_equity=None)
        assert (d.approved, d.reason_code) == (False, "insufficient_equity")


class TestSellSymmetry:
    def test_sell_with_stop_above_entry_approved(self) -> None:
        d = evaluate(action=SignalAction.SELL, entry_price=100.0, stop_loss=101.0)
        assert d.approved

    @pytest.mark.parametrize("action", [SignalAction.BUY, SignalAction.SELL])
    def test_directional_without_stop_never_approved(self, action) -> None:
        d = evaluate(action=action, stop_loss=None)
        assert (d.approved, d.reason_code) == (False, "missing_stop_level")
