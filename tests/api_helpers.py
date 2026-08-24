"""Shared fixtures for the Phase 4 API test suites.

Everything is offline: a scripted market-data provider, a temp cache dir
(via TRADINGAGENTS_CACHE_DIR), and a seeded paper account built through the
real store/engine primitives so responses exercise production code paths.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("TRADINGAGENTS_CACHE_DIR", "")  # ensure key exists

T0 = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)  # Friday morning


class FakeProvider:
    """Deterministic provider: flat candle ladder + fixed quote."""

    name = "fake"

    def __init__(self, *, price: float = 2000.0, bars: int = 300):
        from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries
        from tradingagents.marketdata.timeframes import Timeframe

        self._tf = Timeframe("1h")
        start = T0 - timedelta(hours=bars)
        built = [
            Bar(
                timestamp=start + timedelta(hours=i),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=100.0,
            )
            for i in range(bars)
        ]
        self.series = {
            asset: OhlcvSeries(
                asset_id=asset,
                timeframe=self._tf,
                source="fake",
                status=DataStatus.SIMULATED,
                bars=list(built),
            )
            for asset in ("XAUUSD", "BTCUSD")
        }
        self._price = price

    def get_ohlcv(self, asset, timeframe, *, limit=None, start=None, end=None):
        series = self.series[asset.asset_id]
        bars = series.bars
        if timeframe != series.timeframe:
            # naive resample by repeating the same bar shape on the new grid
            bars = [
                type(series.bars[0])(
                    timestamp=T0 - timedelta(minutes=timeframe.minutes) * (i + 1),
                    open=self._price,
                    high=self._price + 1,
                    low=self._price - 1,
                    close=self._price,
                    volume=100.0,
                )
                for i in range(400)
            ][::-1]
        if limit is not None:
            bars = bars[-limit:]
        return type(series)(
            asset_id=series.asset_id,
            timeframe=timeframe,
            source="fake",
            status=series.status,
            bars=bars,
        )

    def get_quote(self, asset):
        from tradingagents.marketdata.models import DataStatus, Quote

        return Quote(
            asset_id=asset.asset_id,
            timestamp=T0,
            last=self._price if asset.asset_id == "XAUUSD" else 60000.0,
            source="fake",
            status=DataStatus.SIMULATED,
        )


def seed_account(store) -> dict:
    """Seed one closed trade lifecycle through real store writes."""
    from tradingagents.backtest.ledger import TradeRecord
    from tradingagents.paper.models import (
        AccountState,
        DailyPerformanceRow,
        EquitySnapshot,
        JournalEntry,
        JournalNote,
        OrderState,
        PaperOrderEvent,
        PaperSignalRecord,
        ResearchSnapshot,
        SignalState,
    )
    from tradingagents.research.schemas import SignalAction

    now = T0
    state = AccountState(
        account_id=store.base_dir.name.split("/")[-1],
        environment=store._base.parent.name,
        initial_capital=10_000.0,
        cash=10_050.0,
        realized_pnl=50.0,
        total_costs_paid=1.0,
        closed_trades=1,
    )
    store.create_account(state)

    signal_id = "sig-001"
    record = PaperSignalRecord(
        signal_id=signal_id,
        account_id=state.account_id,
        environment=state.environment,
        asset_id="XAUUSD",
        timeframe="1h",
        state=SignalState.EXECUTED,
        decision_bar_close=now,
        generated_at=now,
        market_data_timestamp=now - timedelta(minutes=5),
        action=SignalAction.BUY,
        confidence=0.72,
        thesis="test uptrend",
        supporting_factors=["f1", "f2"],
        entry_reference=2000.0,
        stop_loss_reference=1990.0,
        take_profit_reference=2020.0,
        research=ResearchSnapshot(
            thesis="test uptrend",
            bull_case="bull case text",
            bear_case="bear case text",
            confidence=0.72,
            generated_at=now,
            models_used=["gpt-test"],
            research_version="phase1-research-engine",
        ),
        updated_at=now,
    )
    store.save_signal(record)
    store.append_signal_transition(signal_id=signal_id, from_state="(new)", to_state="generated", reason="", ts=now)
    store.append_signal_transition(
        signal_id=signal_id, from_state="generated", to_state="accepted", reason="risk approved", ts=now
    )
    store.append_signal_transition(
        signal_id=signal_id, from_state="accepted", to_state="executed", reason="filled at next bar open", ts=now
    )

    store.append_order_event(
        PaperOrderEvent(
            ts=now,
            order_id="ord-001",
            signal_id=signal_id,
            account_id=state.account_id,
            asset_id="XAUUSD",
            timeframe="1h",
            action="BUY",
            from_state=OrderState.SIGNAL,
            to_state=OrderState.PENDING,
        )
    )
    store.append_order_event(
        PaperOrderEvent(
            ts=now + timedelta(hours=1),
            order_id="ord-001",
            signal_id=signal_id,
            account_id=state.account_id,
            asset_id="XAUUSD",
            timeframe="1h",
            action="BUY",
            from_state=OrderState.PENDING,
            to_state=OrderState.ACCEPTED,
        )
    )
    store.append_order_event(
        PaperOrderEvent(
            ts=now + timedelta(hours=2),
            order_id="ord-001",
            signal_id=signal_id,
            account_id=state.account_id,
            asset_id="XAUUSD",
            timeframe="1h",
            action="BUY",
            from_state=OrderState.ACCEPTED,
            to_state=OrderState.EXECUTED,
            reason="filled at next bar open",
        )
    )
    store.append_order_event(
        PaperOrderEvent(
            ts=now + timedelta(hours=3),
            order_id="ord-001",
            signal_id=signal_id,
            account_id=state.account_id,
            asset_id="XAUUSD",
            timeframe="1h",
            action="BUY",
            from_state=OrderState.EXECUTED,
            to_state=OrderState.OPEN,
        )
    )

    trade = TradeRecord(
        trade_id=f"{state.environment}-{state.account_id}-00000001",
        run_id=state.environment,
        strategy_id="ai_research",
        asset_id="XAUUSD",
        timeframe="1h",
        direction=1,
        signal_generated_at=now,
        entry_timestamp=now + timedelta(hours=2),
        entry_price=2000.5,
        raw_entry_price=2000.0,
        exit_timestamp=now + timedelta(hours=8),
        exit_price=2010.5,
        raw_exit_price=2010.75,
        quantity=0.45,
        stop_loss=1990.0,
        take_profit=2020.0,
        gross_pnl=51.0,
        transaction_costs=1.0,
        net_pnl=50.0,
        return_pct=0.5549,
        holding_period="PT6H",
        bars_held=6,
        exit_reason="take_profit",
    )
    store.append_trade(trade)

    journal = JournalEntry(
        trade_id=trade.trade_id,
        signal_id=signal_id,
        account_id=state.account_id,
        asset_id="XAUUSD",
        timeframe="1h",
        direction=1,
        opened_at=trade.entry_timestamp,
        closed_at=trade.exit_timestamp,
        exit_reason="take_profit",
        snapshot=record.research,
        notes=[JournalNote(timestamp=trade.exit_timestamp, text="clean breakout")],
    )
    store.save_journal(journal)

    for i in range(3):
        store.append_equity(
            EquitySnapshot(
                timestamp=now.replace(hour=10 + i),
                equity=10_000.0 + i * 25.0,
                cash=10_000.0,
                exposure=0.0,
                open_positions=0,
                drawdown_pct=0.0,
            )
        )
    store.append_daily(
        DailyPerformanceRow(
            date=now.date().isoformat(),
            starting_equity=10_000.0,
            ending_equity=10_050.0,
            daily_return_pct=0.5,
            realized_pnl=50.0,
            trades_closed=1,
            winning_trades=1,
        )
    )
    return {"signal_id": signal_id, "order_id": "ord-001", "trade": trade}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """TestClient over an app whose cache dir is the test tmp path."""
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(tmp_path))
    import importlib

    import tradingagents.default_config as dc

    importlib.reload(dc)  # re-read env override
    monkeypatch.setattr(dc, "DEFAULT_CONFIG", {**dc.DEFAULT_CONFIG, "data_cache_dir": str(tmp_path)})

    from fastapi.testclient import TestClient

    from tradingagents.api.app import ServerSettings, create_app
    from tradingagents.paper.store import JsonPaperStateStore

    store = JsonPaperStateStore(tmp_path / "paper", environment="test", account_id="paper-default")
    seeded = seed_account(store)

    settings = ServerSettings(quote_poll_seconds=0, enable_research_loop=False)
    app = create_app(settings)
    ctx = app.state.ctx
    ctx.provider = lambda: FakeProvider()  # type: ignore[method-assign]
    client = TestClient(app)
    yield client, ctx, tmp_path, seeded
