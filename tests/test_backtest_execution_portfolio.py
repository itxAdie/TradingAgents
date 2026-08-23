"""Phase 2 — execution simulator, portfolio accounting, and trade ledger."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.backtest.config import ExecutionConfig, RiskLimits, SizingPolicy
from tradingagents.backtest.execution import ExecutionSimulator
from tradingagents.backtest.ledger import TradeLedger
from tradingagents.backtest.portfolio import Portfolio, Position, size_position
from tradingagents.marketdata.models import Bar
from tradingagents.research.schemas import (
    DataSourceRef,
    ResearchSignal,
    RiskLevel,
    SignalAction,
)

UTC = timezone.utc


def _ts(i: int) -> datetime:
    return datetime(2025, 4, 1, tzinfo=UTC) + timedelta(hours=i)


def _bar(i: int, open_: float, close: float, high=None, low=None) -> Bar:
    return Bar(
        timestamp=_ts(i), open=open_, high=high or max(open_, close) * 1.001,
        low=low or min(open_, close) * 0.999, close=close, volume=100.0,
    )


def _signal(action: SignalAction, *, stop=None, target=None, i: int = 0) -> ResearchSignal:
    return ResearchSignal(
        asset_id="XAUUSD",
        generated_at=_ts(i),
        timeframe="1h",
        action=action,
        confidence=0.7,
        entry_reference=100.0,
        stop_loss_reference=stop,
        take_profit_reference=target,
        risk_level=RiskLevel.MEDIUM,
        thesis="test",
        supporting_factors=["t"],
        models_used=["m"],
        data_sources=[DataSourceRef(name="replay", kind="market_data", status="historical")],
    )

def _simulator(
    *,
    portfolio: Portfolio | None = None, ledger: TradeLedger | None = None,
    cfg: ExecutionConfig | None = None, limits: RiskLimits | None = None,
    sizing: SizingPolicy | None = None,
) -> ExecutionSimulator:
    # NOTE: explicit None checks — TradeLedger defines __len__, so an empty
    # ledger is falsy and `x or default` would silently replace it.
    return ExecutionSimulator(
        run_id="t-run", strategy_id="s1", asset_id="XAUUSD",
        timeframe="1h", timeframe_minutes=60,
        execution_cfg=cfg if cfg is not None else ExecutionConfig(),
        sizing=sizing if sizing is not None else SizingPolicy(mode="fixed_notional", value=1_000.0),
        limits=limits if limits is not None else RiskLimits(max_position_notional=10_000.0),
        portfolio=portfolio if portfolio is not None else Portfolio(initial_capital=10_000.0),
        ledger=ledger if ledger is not None else TradeLedger(run_id="t-run"),
    )


# -- scheduling and fills ---------------------------------------------------------


def test_hold_never_schedules_a_fill() -> None:
    sim = _simulator()
    sim.schedule_signal(_signal(SignalAction.HOLD), now=_ts(0))
    assert sim.pending is None


def test_entry_fills_at_next_bar_open_not_decision_close() -> None:
    """The walker calls ``on_bar_open`` only for bars after the decision bar.

    The first such call is the next bar's open — the fill must use THAT raw
    price, never the decision candle's close (no same-candle execution).
    """
    portfolio = Portfolio(initial_capital=10_000.0)
    sim = _simulator(portfolio=portfolio)
    sim.schedule_signal(
        _signal(SignalAction.BUY, stop=95.0, target=110.0, i=0), now=_ts(0),
    )

    # First walked bar AFTER the decision close: fills here, at its open.
    sim.on_bar_open(_bar(1, 102.0, 103.0))
    pos = portfolio.positions["XAUUSD"]
    assert pos.direction == 1
    # Adverse fill: long pays above the raw open (slippage + half spread).
    cost_frac = ExecutionConfig().cost_bps_per_side() / 10_000
    assert pos.entry_price == pytest.approx(102.0 * (1 + cost_frac))
    assert pos.raw_entry_price == 102.0
    assert pos.stop_loss is not None and pos.take_profit is not None


def test_pending_intent_replaced_by_newest_signal() -> None:
    sim = _simulator()
    sim.schedule_signal(_signal(SignalAction.BUY, i=0), now=_ts(0))
    sim.schedule_signal(_signal(SignalAction.SELL, i=1), now=_ts(1))
    assert sim.pending is not None and sim.pending.signal.action is SignalAction.SELL


def test_flip_exits_then_enters_at_same_open() -> None:
    portfolio = Portfolio(initial_capital=10_000.0)
    ledger = TradeLedger(run_id="t-run")
    sim = _simulator(portfolio=portfolio, ledger=ledger)

    sim.schedule_signal(_signal(SignalAction.BUY, i=0), now=_ts(0))
    sim.on_bar_open(_bar(1, 100.0, 101.0))  # long opened
    assert portfolio.positions["XAUUSD"].direction == 1

    sim.schedule_signal(_signal(SignalAction.SELL, i=2), now=_ts(2))
    sim.on_bar_open(_bar(3, 105.0, 104.0))  # flip bar

    assert portfolio.positions["XAUUSD"].direction == -1
    assert len(ledger.records) == 1  # the exit leg recorded
    rec = ledger.records[0]
    assert rec.exit_reason == "signal_exit"
    assert rec.exit_timestamp == _ts(3)


def test_stop_wins_when_stop_and_target_touch_same_bar() -> None:
    portfolio = Portfolio(initial_capital=10_000.0)
    ledger = TradeLedger(run_id="t-run")
    sim = _simulator(portfolio=portfolio, ledger=ledger)

    sim.schedule_signal(
        _signal(SignalAction.BUY, stop=95.0, target=110.0, i=0), now=_ts(0),
    )
    sim.on_bar_open(_bar(1, 100.0, 108.0))
    # One huge bar touching BOTH levels; pessimistic rule => stop wins.
    huge = _bar(2, 108.0, 109.0, high=111.0, low=94.0)
    reason = sim.on_bar_close(huge)
    assert reason == "stop_loss"
    assert "XAUUSD" not in portfolio.positions
    assert ledger.records[0].exit_reason == "stop_loss"


def test_take_profit_fires_when_only_target_touched() -> None:
    portfolio = Portfolio(initial_capital=10_000.0)
    ledger = TradeLedger(run_id="t-run")
    sim = _simulator(portfolio=portfolio, ledger=ledger)

    sim.schedule_signal(
        _signal(SignalAction.BUY, stop=90.0, target=105.0, i=0), now=_ts(0),
    )
    sim.on_bar_open(_bar(1, 100.0, 102.0))
    reason = sim.on_bar_close(_bar(2, 102.0, 104.5, high=106.0, low=101.5))
    assert reason == "take_profit"
    rec = ledger.records[0]
    assert rec.gross_pnl > 0


def test_end_of_data_settlement_labelled_honestly() -> None:
    portfolio = Portfolio(initial_capital=10_000.0)
    ledger = TradeLedger(run_id="t-run")
    sim = _simulator(portfolio=portfolio, ledger=ledger)

    sim.schedule_signal(_signal(SignalAction.BUY, stop=50.0, target=200.0, i=0), now=_ts(0))
    sim.on_bar_open(_bar(1, 100.0, 100.5))
    last = _bar(9, 101.0, 99.0)
    sim.force_close_last_bar(last)

    rec = ledger.records[0]
    assert rec.exit_reason == "end_of_data"
    assert rec.exit_timestamp == last.timestamp
    assert "XAUUSD" not in portfolio.positions
    assert sim.pending is None


def test_risk_limit_rejection_counts_and_skips_fill() -> None:
    portfolio = Portfolio(initial_capital=10_000.0)
    # Exposure cap effectively zero: every entry must be rejected.
    limits = RiskLimits(
        max_open_positions=5, max_position_notional=1e12,
        max_total_exposure_pct=1e-9,
    )
    sim = _simulator(portfolio=portfolio, limits=limits)

    sim.schedule_signal(_signal(SignalAction.BUY, i=0), now=_ts(0))
    sim.on_bar_open(_bar(1, 100.0, 101.0))
    assert sim.rejected_entries == 1
    assert "XAUUSD" not in portfolio.positions


def test_zero_cash_blocks_new_entries() -> None:
    portfolio = Portfolio(initial_capital=10_000.0)
    portfolio.cash = -1.0  # settled capital exhausted
    limits = RiskLimits(max_open_positions=5)
    sim = _simulator(portfolio=portfolio, limits=limits)
    sim.schedule_signal(_signal(SignalAction.BUY, i=0), now=_ts(0))
    sim.on_bar_open(_bar(1, 100.0, 100.0))
    assert sim.rejected_entries == 1
    assert "XAUUSD" not in portfolio.positions


def test_short_entry_pays_adverse_costs_too() -> None:
    portfolio = Portfolio(initial_capital=10_000.0)
    sim = _simulator(portfolio=portfolio)
    sim.schedule_signal(_signal(SignalAction.SELL, i=0), now=_ts(0))
    sim.on_bar_open(_bar(1, 100.0, 99.0))
    pos = portfolio.positions["XAUUSD"]
    cost_frac = ExecutionConfig().cost_bps_per_side() / 10_000
    assert pos.direction == -1
    # Short sells BELOW the raw open (adverse direction).
    assert pos.entry_price == pytest.approx(100.0 * (1 - cost_frac))


# -- portfolio accounting -----------------------------------------------------------


def test_portfolio_settled_cash_semantics() -> None:
    p = Portfolio(initial_capital=10_000.0)
    pos = Position(
        asset_id="XAUUSD", direction=1, quantity=10.0,
        entry_price=100.0, entry_time=_ts(0),
    )
    p.open_position(pos)
    # Cash unchanged while position open; equity marks to market.
    assert p.cash == 10_000.0
    # equity = cash + (mark - entry) * qty = 10000 + 100
    assert p.equity({"XAUUSD": 110.0}) == pytest.approx(10_100.0)
    assert p.unrealized_pnl({"XAUUSD": 110.0}) == pytest.approx(100.0)
    assert p.gross_exposure({"XAUUSD": 110.0}) == pytest.approx(1_100.0)

    closed = p.close_position("XAUUSD", net_pnl=90.0, costs_paid=10.0)
    assert closed is pos
    assert p.cash == pytest.approx(10_090.0)
    assert p.realized_pnl == pytest.approx(90.0)
    assert p.total_costs_paid == pytest.approx(10.0)
    assert p.closed_trades == 1
    assert p.equity({}) == pytest.approx(10_090.0)


def test_portfolio_rejects_pyramiding_and_bad_direction() -> None:
    p = Portfolio(initial_capital=1_000.0)
    pos = Position(
        asset_id="X", direction=1, quantity=1.0,
        entry_price=10.0, entry_time=_ts(0),
    )
    p.open_position(pos)
    with pytest.raises(ValueError, match="pyramiding"):
        p.open_position(
            Position(asset_id="X", direction=-1, quantity=1.0,
                     entry_price=10.0, entry_time=_ts(1)),
        )
    with pytest.raises(ValueError, match="direction"):
        Position(asset_id="Y", direction=0, quantity=1.0,
                 entry_price=10.0, entry_time=_ts(0))
    with pytest.raises(ValueError, match="positive"):
        Position(asset_id="Z", direction=1, quantity=0.0,
                 entry_price=10.0, entry_time=_ts(0))


def test_size_position_modes() -> None:
    fixed_q = size_position(
        policy=SizingPolicy(mode="fixed_quantity", value=2.5),
        equity=10_000.0, price=50.0,
    )
    assert fixed_q == 2.5
    fixed_n = size_position(
        policy=SizingPolicy(mode="fixed_notional", value=500.0),
        equity=10_000.0, price=25.0,
    )
    assert fixed_n == pytest.approx(20.0)
    pct = size_position(
        policy=SizingPolicy(mode="pct_equity", value=0.5),
        equity=10_000.0, price=100.0,
    )
    assert pct == pytest.approx(50.0)
    with pytest.raises(ValueError):
        size_position(
            policy=SizingPolicy(mode="fixed_quantity", value=0.0),
            equity=1.0, price=1.0,
        )


# -- ledger export --------------------------------------------------------------------


def test_ledger_json_and_csv_export(tmp_path) -> None:
    from tradingagents.backtest.execution import ExecutionSimulator  # noqa: F401

    portfolio = Portfolio(initial_capital=10_000.0)
    ledger = TradeLedger(run_id="run-42")
    sim = _simulator(portfolio=portfolio, ledger=ledger)
    sim.schedule_signal(_signal(SignalAction.BUY, stop=90.0, target=120.0, i=0), now=_ts(0))
    sim.on_bar_open(_bar(1, 100.0, 101.0))
    sim.force_close_last_bar(_bar(5, 102.0, 103.0))

    json_path = ledger.to_json(tmp_path / "trades.json")
    csv_path = ledger.to_csv(tmp_path / "trades.csv")
    assert json_path.exists() and csv_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    # Ledger JSON is a bare array of trade records.
    assert isinstance(payload, list) and len(payload) == 1
    trade = payload[0]
    assert trade["trade_id"] == "run-42-T0001"
    assert trade["exit_reason"] == "end_of_data"

    csv_text = csv_path.read_text(encoding="utf-8")
    assert "trade_id" in csv_text.splitlines()[0]
    assert "run-42-T0001" in csv_text
