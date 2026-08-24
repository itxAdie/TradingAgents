"""PaperTradingConfig safety properties (ARCHITECTURE.md P3.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tradingagents.paper.config import (
    DEFAULT_INITIAL_CAPITAL,
    PaperRiskLimits,
    PaperTradingConfig,
    ScheduleEntry,
)


def test_live_environment_is_not_constructible() -> None:
    with pytest.raises(ValidationError, match="literal_error"):
        PaperTradingConfig(environment="live")


def test_test_and_paper_environments_accepted() -> None:
    assert PaperTradingConfig(environment="test").environment == "test"
    assert PaperTradingConfig(environment="paper").environment == "paper"


def test_kill_switch_defaults_to_off() -> None:
    assert PaperTradingConfig().enabled is False


def test_account_id_is_path_safe() -> None:
    for bad in ("../evil", "Aupper", "-leadingdash", "", "has space", "x" * 65):
        with pytest.raises(ValidationError):
            PaperTradingConfig(account_id=bad)
    ok = PaperTradingConfig(account_id="gold-1h_01")
    assert ok.account_id == "gold-1h_01"


def test_schedule_entry_validates_against_registry_and_timeframes() -> None:
    entry = ScheduleEntry(asset_id="XAUUSD", timeframe="1H")  # case-insensitive
    assert entry.timeframe == "1h"
    with pytest.raises(ValidationError):
        ScheduleEntry(asset_id="NOPE_COIN", timeframe="1h")
    with pytest.raises(ValidationError):
        ScheduleEntry(asset_id="XAUUSD", timeframe="2h")
    with pytest.raises(ValidationError):
        ScheduleEntry(asset_id="XAUUSD", timeframe="1h", offset_minutes=61)
    with pytest.raises(ValidationError):
        ScheduleEntry(asset_id="XAUUSD", timeframe="1h", offset_minutes=-1)


def test_stale_overrides_must_reference_real_timeframes() -> None:
    cfg = PaperTradingConfig(stale_overrides_hours={"1h": 4.0})
    assert cfg.stale_overrides_hours == {"1h": 4.0}
    with pytest.raises(ValidationError):
        PaperTradingConfig(stale_overrides_hours={"7h": 4.0})
    with pytest.raises(ValidationError):
        PaperTradingConfig(stale_overrides_hours={"1h": -1.0})


def test_risk_limits_inherit_backtest_caps() -> None:
    limits = PaperRiskLimits()
    # inherited from backtest RiskLimits
    assert limits.max_position_notional > 0
    assert limits.max_open_positions >= 1
    assert limits.max_total_exposure_pct <= 100
    # paper-specific
    assert limits.max_risk_per_trade_pct == pytest.approx(0.01)
    assert limits.max_daily_loss_pct == pytest.approx(0.03)
    assert limits.max_drawdown_pct == pytest.approx(0.20)
    with pytest.raises(ValidationError):
        PaperRiskLimits(max_drawdown_pct=1.5)


def test_default_capital_positive() -> None:
    cfg = PaperTradingConfig()
    assert cfg.initial_capital == DEFAULT_INITIAL_CAPITAL


def test_schedule_for_lookup() -> None:
    cfg = PaperTradingConfig(
        schedules=[
            ScheduleEntry(asset_id="XAUUSD", timeframe="1h"),
            ScheduleEntry(asset_id="BTCUSD", timeframe="15m", enabled=False),
        ]
    )
    assert cfg.schedule_for("XAUUSD", "1h") is not None
    assert cfg.schedule_for("XAUUSD", "4h") is None
    assert cfg.schedule_for("GOLD", "1h") is not None  # alias resolution
    assert cfg.schedule_for("BTCUSD", "15m") is None  # disabled slot excluded
