"""API market + research + system endpoints (offline, scripted provider)."""

from __future__ import annotations

from datetime import timedelta

from tests.api_helpers import T0  # noqa: F401 - fixture via conftest
from tradingagents.research.assembly import AssembledResult


def _seed_research_run(ctx, *, asset_id="XAUUSD", action="BUY", confidence=0.72):
    from tradingagents.research.schemas import (
        BearCase,
        BullCase,
        ResearchManagerVerdict,
        ResearchReport,
        ResearchSignal,
        RiskLevel,
        SignalAction,
    )

    report = ResearchReport(
        asset_id=asset_id,
        display_name=asset_id,
        timeframe="1h",
        generated_at=T0,
        market_data_timestamp=T0 - timedelta(minutes=5),
        market_data_status="simulated",
        technical_analysis=None,
        bull_case=BullCase(thesis="up", key_points=["a", "b"], relies_on=["r"]),
        bear_case=BearCase(thesis="down", key_points=["c", "d"], relies_on=["s"]),
        manager_verdict=ResearchManagerVerdict(
            direction="BUY",
            consensus="bulls win",
            invalidation_conditions=["break below 1990"],
            risks=["fed surprise"],
        ),
        confidence=confidence,
        models_used=["gpt-test"],
    )
    signal = ResearchSignal(
        asset_id=asset_id,
        generated_at=T0,
        timeframe="1h",
        action=SignalAction(action),
        confidence=confidence,
        risk_level=RiskLevel.LOW if confidence >= 0.6 else RiskLevel.MEDIUM,
        thesis="test",
        supporting_factors=["f1", "f2"],
    )
    summary = ctx.research_store.save_result(
        AssembledResult(report=report, signal=signal),
        model_ids=["gpt-test"],
    )
    return summary.run_id


def test_market_overview_lists_registered_assets(api):
    client, _, _, _ = api
    r = client.get("/api/markets")
    assert r.status_code == 200
    items = {i["spec"]["asset_id"]: i for i in r.json()}
    assert "XAUUSD" in items and "BTCUSD" in items
    xau = items["XAUUSD"]
    assert xau["quote"]["last"] == 2000.0
    assert xau["change_pct"] == 0.0  # flat ladder: last two closes equal


def test_market_detail_includes_refs_and_slots(api):
    client, ctx, _, seeded = api
    run_id = _seed_research_run(ctx)
    r = client.get("/api/markets/XAUUSD")
    assert r.status_code == 200
    body = r.json()
    assert body["freshness"] in ("fresh", "stale", "unknown")
    assert body["latest_signal_ref"]["signal_id"] == seeded["signal_id"]
    assert body["latest_research_run"]["run_id"] == run_id
    # server schedules cover XAUUSD on all four timeframes
    assert len(body["scheduled_slots"]) == 4
    assert all(slot["enabled"] is False for slot in body["scheduled_slots"])


def test_market_detail_unknown_asset_404(api):
    client, *_ = api
    r = client.get("/api/markets/NOPE")
    assert r.status_code == 404


def test_candles_shape_and_timeframe_validation(api):
    client, *_ = api
    r = client.get("/api/markets/XAUUSD/candles", params={"timeframe": "1h", "limit": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["timeframe"] == "1h"
    assert len(body["bars"]) == 50
    bar = body["bars"][0]
    for key in ("t", "open", "high", "low", "close"):
        assert key in bar
    bad = client.get("/api/markets/XAUUSD/candles", params={"timeframe": "7m"})
    assert bad.status_code == 400


def test_indicators_return_backend_computed_series(api):
    client, *_ = api
    r = client.get(
        "/api/markets/XAUUSD/indicators",
        params={"kinds": "ema20,sma50,bb20-lower,bogus9", "limit": 100},
    )
    assert r.status_code == 200
    body = r.json()
    names = [s["name"] for s in body["series"]]
    assert names == ["ema20", "sma50", "bb20-lower"]
    assert "bogus9" in body["na_reasons"]
    ema = body["series"][0]["values"]
    assert len(ema) == 100
    assert ema[0] is not None  # ewm defined from the first point
    assert ema[-1] == 2000.0  # flat ladder converges to the price


def test_research_list_then_full_report(api):
    client, ctx, _, _ = api
    run_id = _seed_research_run(ctx)
    listed = client.get("/api/research").json()
    assert listed["total"] == 1
    row = listed["items"][0]
    assert row["run_id"] == run_id and row["signal_action"] == "BUY"

    detail = client.get(f"/api/research/{run_id}").json()
    assert detail["report"]["bull_case"]["thesis"] == "up"
    assert detail["report"]["manager_verdict"]["consensus"] == "bulls win"
    assert detail["signal"]["action"] == "BUY"

    missing = client.get("/api/research/rs-does-not-exist")
    assert missing.status_code == 404


def test_research_filters(api):
    client, ctx, _, _ = api
    _seed_research_run(ctx)
    _seed_research_run(ctx, asset_id="BTCUSD", action="SELL", confidence=0.2)
    gold_buys = client.get(
        "/api/research", params={"asset_id": "GOLD"}  # alias normalised? no — raw filter
    )
    assert gold_buys.status_code == 200
    sells = client.get("/api/research", params={"action": "sell"})
    assert sells.json()["total"] == 1
    assert sells.json()["items"][0]["asset_id"] == "BTCUSD"


def test_system_status_components(api):
    client, ctx, _, _ = api
    r = client.get("/api/system/status")
    assert r.status_code == 200
    components = {c["component"]: c for c in r.json()["components"]}
    expected = {
        "Backend", "Market Data", "AI Research", "Database",
        "Scheduler", "Paper Trading", "Realtime Connection",
    }
    assert expected <= set(components)
    assert components["AI Research"]["status"] in ("enabled", "disabled")
    detail = str(components["AI Research"]).lower()
    assert "sk-" not in detail  # never leak key material shapes
