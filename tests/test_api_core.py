"""API core: envelopes, error mapping, auth guard, settings/audit surfaces."""

from __future__ import annotations


def test_root_service_info_without_dashboard(api):
    client, _, _, _ = api
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert "PAPER" in body["mode"].upper()
    assert "SIMULATED" in body["mode"].upper()


def test_unknown_route_returns_envelope_error(api):
    client, *_ = api
    r = client.get("/api/definitely-not-a-route")
    # FastAPI's own 404 is fine; envelope shape matters for our handlers
    assert r.status_code == 404


def test_validation_error_is_enveloped(api):
    client, *_ = api
    r = client.get("/api/signals", params={"confidence_min": "not-a-float"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_request"


def test_pagination_params_validated(api):
    client, *_ = api
    assert client.get("/api/signals", params={"limit": 0}).status_code == 422
    assert client.get("/api/trades", params={"offset": -1}).status_code == 422


def test_auth_guard_blocks_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(tmp_path))
    from tradingagents.api.app import ServerSettings, create_app

    app = create_app(
        ServerSettings(quote_poll_seconds=0, api_token="secret-token")
    )
    from fastapi.testclient import TestClient

    client = TestClient(app)
    denied = client.get("/api/system/status")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"
    allowed = client.get(
        "/api/system/status", headers={"Authorization": "Bearer secret-token"}
    )
    assert allowed.status_code == 200
    # non-/api paths stay open (dashboard assets)
    assert client.get("/").status_code == 200


def test_settings_view_hides_secrets_and_lists_safe_fields(api):
    client, ctx, _, _ = api
    r = client.get("/api/system/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_enabled"] is False
    assert "XAUUSD" in body["assets"] and "BTCUSD" in body["assets"]
    text = str(body).lower()
    for forbidden in ("api_key", "apikey", "secret", "password"):
        assert forbidden not in text


def test_audit_log_records_and_tails(api):
    client, ctx, _, _ = api
    ctx.audit.record("test_action", foo="bar")
    r = client.get("/api/system/audit")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["action"] == "test_action"
    assert items[0]["detail"]["foo"] == "bar"


def test_no_account_maps_to_404_envelope(api):
    client, _, _, _ = api
    r = client.get("/api/portfolio", params={"account": "missing-account"})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "no_account"
