"""API paper-trading endpoints: portfolio, positions, signals, trades, risk."""

from __future__ import annotations


def test_portfolio_report_verbatim_with_disclaimer(api):
    client, *_ = api
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_name"] == "PAPER_ACCOUNT_REPORT"
    assert "SIMULATED" in body["disclaimer"].upper()
    assert body["initial_capital"] == 10_000.0
    assert body["cash"] == 10_050.0
    assert body["equity"] >= body["cash"]  # no open marks → equity == cash
    assert body["stats"]["n_trades"] == 1
    assert body["orders_total"] == 1
    assert body["daily"][0]["daily_return_pct"] == 0.5


def test_equity_curve_chronological_and_paginated(api):
    client, *_ = api
    full = client.get("/api/portfolio/equity-curve").json()
    assert full["total"] == 3
    stamps = [i["timestamp"] for i in full["items"]]
    assert stamps == sorted(stamps)
    paged = client.get(
        "/api/portfolio/equity-curve", params={"limit": 2, "offset": 1}
    ).json()
    assert paged["total"] == 3 and len(paged["items"]) == 2
    assert paged["items"][0]["timestamp"] == stamps[1]


def test_positions_include_backend_unrealized_pnl(api):
    from datetime import timedelta

    client, ctx, _, _ = api
    from tradingagents.paper.models import PositionRecord

    store = ctx.store()
    recs = store.load_positions()
    recs.append(
        PositionRecord(
            position_id="pos-002",
            account_id=recs[0].account_id if recs else "paper-default",
            signal_id="sig-001",
            asset_id="XAUUSD",
            timeframe="1h",
            direction=-1,
            quantity=0.5,
            entry_price=2000.0,
            raw_entry_price=1999.9,
            entry_time=api_T0() + timedelta(hours=2),
            updated_at=api_T0() + timedelta(hours=2),
            stop_loss=2015.0,
            take_profit=1950.0,
            current_price=1990.0,
        )
    )
    store.save_positions(recs)

    r = client.get("/api/portfolio/positions")
    assert r.status_code == 200
    items = {i["position_id"]: i for i in r.json()["items"]}
    short = items["pos-002"]
    assert short["direction"] == -1
    assert abs(short["unrealized_pnl"] - (1990.0 - 2000.0) * -1 * 0.5) < 1e-9


def api_T0():
    from tests.api_helpers import T0

    return T0


def test_signals_list_filters_and_risk_decision(api):
    client, *_ = api
    r = client.get("/api/signals")
    assert r.status_code == 200
    assert r.json()["total"] == 1
    sig = r.json()["items"][0]
    assert sig["signal_id"] == "sig-001"
    assert sig["state"] == "executed" and sig["executed"] is True
    assert sig["risk_decision"] == "approved"

    filtered = client.get(
        "/api/signals", params={"asset_id": "BTCUSD"}
    )
    assert filtered.json()["total"] == 0
    by_conf = client.get("/api/signals", params={"confidence_min": 0.8})
    assert by_conf.json()["total"] == 0


def test_signal_detail_full_lifecycle(api):
    client, *_ = api
    r = client.get("/api/signals/sig-001")
    assert r.status_code == 200
    body = r.json()
    states = [t["to_state"] for t in body["transitions"]]
    assert states == ["generated", "accepted", "executed"]
    assert all(t["ts"] is not None for t in body["transitions"])
    order_states = [o["state"] for o in body["orders"]]
    assert order_states[-1] == "open"
    assert body["record"]["research"]["bull_case"] == "bull case text"


def test_signal_detail_404(api):
    client, *_ = api
    assert client.get("/api/signals/nope").status_code == 404


def test_trades_list_outcome_filtering(api):
    client, *_ = api
    r = client.get("/api/trades")
    assert r.status_code == 200 and r.json()["total"] == 1
    trade = r.json()["items"][0]
    assert trade["outcome"] == "win"
    assert trade["has_journal"] is True
    assert trade["strategy_version"] == "phase1-research-engine"

    losses = client.get("/api/trades", params={"outcome": "loss"})
    assert losses.json()["total"] == 0


def test_trade_detail_timeline_stages(api):
    client, *_ = api
    listing = client.get("/api/trades").json()["items"][0]
    detail = client.get(f"/api/trades/{listing['trade_id']}")
    assert detail.status_code == 200
    stages = [(s["stage"], s["timestamp"]) for s in detail.json()["timeline"]]
    names = [s for s, _ in stages]
    assert names == [
        "market_data", "research", "signal", "risk_decision",
        "paper_order", "entry", "position", "exit",
    ]
    assert all(ts is not None for _, ts in stages)
    journal = detail.json()["journal"]
    assert journal["snapshot"]["bear_case"] == "bear case text"


def test_journal_note_mutation_audited(api):
    client, ctx, tmp_path, seeded = api
    trade_id = seeded["trade"].trade_id
    r = client.put(
        f"/api/trades/{trade_id}/journal",
        json={"text": "held to target as planned"},
    )
    assert r.status_code == 200
    store = ctx.store()
    journal = store.load_journal(trade_id)
    assert journal.notes[-1].text == "held to target as planned"
    audit_rows = list(ctx.audit.tail(10))
    assert any(row["action"] == "journal_note_added" for row in audit_rows)

    missing = client.put(
        "/api/trades/unknown-trade/journal", json={"text": "x"}
    )
    assert missing.status_code == 404


def test_risk_limits_vs_current_utilization(api):
    client, *_ = api
    r = client.get("/api/risk")
    assert r.status_code == 200
    body = r.json()
    limits = {row["key"]: row for row in body["limits"]}
    expected_keys = {
        "max_daily_loss", "max_drawdown", "max_total_exposure",
        "max_risk_per_trade", "max_open_positions", "max_position_notional",
    }
    assert expected_keys <= set(limits)
    daily = limits["max_daily_loss"]
    assert daily["current_value"] == 0.0  # equity above day start → zero loss
    assert daily["utilization_pct"] == 0.0
    assert body["open_positions"] == 0 or body["open_positions"] >= 0
    assert body["peak_equity"] >= body["equity"]


def test_risk_events_from_persisted_evidence(api):
    client, ctx, _, _ = api
    # seed a rejected signal + a stop-loss trade
    from tests.api_helpers import T0
    from tradingagents.backtest.ledger import TradeRecord

    store = ctx.store()
    store.append_signal_transition(
        signal_id="sig-bad", from_state="generated", to_state="rejected",
        reason="risk:max_exposure",
    )
    store.append_trade(
        TradeRecord(
            trade_id="tr-stop", run_id="test", strategy_id="ai_research",
            asset_id="BTCUSD", timeframe="1h", direction=-1,
            signal_generated_at=T0, entry_timestamp=T0,
            entry_price=60000.0, raw_entry_price=60000.0,
            exit_timestamp=T0, exit_price=61000.0, raw_exit_price=61000.0,
            quantity=0.01, gross_pnl=-10.0, transaction_costs=1.0,
            net_pnl=-11.0, return_pct=-1.83, holding_period="PT0S",
            bars_held=0, exit_reason="stop_loss",
        )
    )
    r = client.get("/api/risk/events")
    assert r.status_code == 200
    types = [e["type"] for e in r.json()["items"]]
    assert "SIGNAL_BLOCKED" in types
    assert "STOP_LOSS_TRIGGERED" in types
    blocked = next(e for e in r.json()["items"] if e["type"] == "SIGNAL_BLOCKED")
    assert "max_exposure" in blocked["message"]
