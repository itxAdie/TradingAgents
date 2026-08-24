"""Backtest registry + realtime (SSE/history) endpoints."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from tests.api_helpers import T0  # noqa: F401 - fixture via conftest


def _synthetic_dataset(tmp_path, monkeypatch, *, n_bars=520):
    """Store a small dataset the registry can run baselines over."""
    from tradingagents.backtest.historical.store import JsonDataStore
    from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries
    from tradingagents.marketdata.timeframes import Timeframe

    bars = []
    price = 2000.0
    for i in range(n_bars):
        step = 0.5 if i % 10 < 5 else -0.4
        nxt = price + step
        bars.append(
            Bar(
                timestamp=T0 - timedelta(hours=n_bars - i),
                open=price,
                high=max(price, nxt) + 0.3,
                low=min(price, nxt) - 0.3,
                close=nxt,
                volume=10.0,
            )
        )
        price = nxt
    store = JsonDataStore(tmp_path / "historical")
    series = OhlcvSeries(
        asset_id="XAUUSD",
        timeframe=Timeframe("1h"),
        source="synthetic",
        status=DataStatus.SIMULATED,
        bars=bars,
    )
    from tradingagents.backtest.historical.store import build_meta

    meta = build_meta(series, provider_symbol="GC=F")
    path = store.save(series, meta)
    return path


def test_backtest_submit_run_and_report(api, monkeypatch):
    client, ctx, tmp_path, _ = api
    _synthetic_dataset(tmp_path, monkeypatch)

    r = client.post(
        "/api/backtests",
        json={
            "asset_id": "XAUUSD",
            "timeframe": "1h",
            "start": (T0 - timedelta(days=10)).date().isoformat(),
            "end": T0.date().isoformat(),
            "include_walk_forward": True,
        },
    )
    assert r.status_code == 202
    job = r.json()
    run_id = job["run_id"]
    assert job["status"] in ("queued", "running", "completed")

    for _ in range(600):
        detail = client.get(f"/api/backtests/{run_id}").json()
        if detail["job"]["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert detail["job"]["status"] == "completed", detail["job"]["error"]
    report = detail["report"]
    assert report is not None
    assert report["schema_name"] == "BACKTEST_REPORT"
    assert report["run_id"] == run_id
    strategy_ids = {s["strategy_id"] for s in report["strategies"]}
    assert {"baseline_buy_hold", "baseline_sma_cross", "baseline_momentum"} <= strategy_ids
    assert report["walk_forward"], "walk-forward windows attached"

    listing = client.get("/api/backtests").json()
    assert listing["total"] == 1
    assert any(audit["action"] == "backtest_submitted" for audit in ctx.audit.tail(20))


def test_backtest_validation_errors(api):
    client, *_ = api
    bad_asset = client.post(
        "/api/backtests", json={"asset_id": "NOPE", "timeframe": "1h",
                                "start": "2026-01-01", "end": "2026-02-01"}
    )
    assert bad_asset.status_code == 400
    bad_tf = client.post(
        "/api/backtests", json={"asset_id": "XAUUSD", "timeframe": "7m",
                                "start": "2026-01-01", "end": "2026-02-01"}
    )
    assert bad_tf.status_code == 400
    bad_dates = client.post(
        "/api/backtests", json={"asset_id": "XAUUSD", "timeframe": "1h",
                                "start": "2026-02-01", "end": "2026-01-01"}
    )
    assert bad_dates.status_code == 400


def test_backtest_missing_dataset_fails_job_honestly(api):
    client, ctx, _, _ = api
    r = client.post(
        "/api/backtests",
        json={"asset_id": "XAUUSD", "timeframe": "1h",
              "start": "2026-01-01", "end": "2026-02-01"},
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    for _ in range(100):
        job = client.get(f"/api/backtests/{run_id}").json()["job"]
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "failed"
    assert "fetch" in job["error"].lower()


def test_backtest_404(api):
    client, *_ = api
    assert client.get("/api/backtests/bt-missing").status_code == 404


# ---------------------------------------------------------------------------
# Realtime: bus history + SSE stream


try:
    import httpx2 as httpx_lib
except ImportError:  # pragma: no cover
    import httpx as httpx_lib


@pytest.fixture()
def live(api):
    """Real uvicorn server on an ephemeral port (lifespan off).

    The bundled TestClient executes the ASGI app to completion inside a
    blocking portal call, which deadlocks on never-ending SSE responses;
    only a genuine socket exercises the stream path honestly.
    """
    client, _, _, _ = api
    import threading

    import uvicorn

    config = uvicorn.Config(
        client.app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    # works for both asyncio and uvloop server wrappers
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_event_history_reflects_bus(api):
    client, ctx, _, _ = api
    ctx.bus.notify("price_updated", {"asset_id": "XAUUSD", "last": 2001.0})
    ctx.bus.publish({"event": "audit_event", "ts": "t", "action": "x"})
    rows = client.get("/api/events/history").json()["items"]
    events = [r["event"] for r in rows]
    assert "price_updated" in events and "audit_event" in events


def test_sse_stream_delivers_events_then_disconnects(live, api):
    _, ctx, _, _ = api

    with httpx_lib.Client(base_url=live, timeout=20) as hc, hc.stream(
        "GET", "/api/events/stream", params={"replay": 0}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # subscribed once headers arrive; publishing now must be seen
        ctx.bus.notify("signal_generated", {"asset_id": "BTCUSD"})
        got_signal = False
        for raw_line in response.iter_lines():
            if raw_line.startswith("event: signal_generated"):
                got_signal = True
                break
        assert got_signal


def test_sse_replay_history_first(live, api):
    _, ctx, _, _ = api
    ctx.bus.notify("price_updated", {"asset_id": "XAUUSD", "last": 1999.0})

    with httpx_lib.Client(base_url=live, timeout=20) as hc, hc.stream(
        "GET", "/api/events/stream", params={"replay": 5}
    ) as response:
        chunks = []
        for raw_line in response.iter_lines():
            chunks.append(raw_line)
            if len(chunks) > 6:
                break
    text = "\n".join(chunks)
    assert '"event":"price_updated"' in text or "price_updated" in text


def test_sse_payload_is_single_line_json(live, api):
    _, ctx, _, _ = api
    ctx.bus.publish({"ts": "now", "event": "multi", "payload": {"a\nb": 1}})

    with httpx_lib.Client(base_url=live, timeout=20) as hc, hc.stream(
        "GET", "/api/events/stream", params={"replay": 1}
    ) as response:
        lines = []
        for raw_line in response.iter_lines():
            lines.append(raw_line)
            if raw_line.startswith("data:"):
                break
    data_line = next(ln for ln in lines if ln.startswith("data:"))
    payload = json.loads(data_line[len("data:"):])
    assert payload["event"] == "multi"
