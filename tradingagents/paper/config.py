"""Paper-trading configuration.

Pure configuration objects in the same style as
:mod:`tradingagents.backtest.config` (pydantic, validated, no YAML, no ad-hoc
``os.getenv``). Two safety properties are structural:

- ``environment`` is ``Literal["test", "paper"]`` — a ``"live"`` value cannot
  be constructed (ARCHITECTURE.md P3.2).
- ``enabled`` defaults to **False**: the kill switch must be armed explicitly
  before any cycle can trade.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from tradingagents.assets.registry import UnknownAssetError, get_asset
from tradingagents.backtest.config import ExecutionConfig, RiskLimits, SizingPolicy
from tradingagents.marketdata.timeframes import Timeframe

ENV_TEST = "test"
ENV_PAPER = "paper"

Environment = Literal["test", "paper"]

_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DEFAULT_ACCOUNT_ID = "paper-default"
DEFAULT_INITIAL_CAPITAL = 10_000.0


class PaperRiskLimits(RiskLimits):
    """Backtest ``RiskLimits`` plus paper-specific deterministic caps.

    All fraction-valued fields are fractions of equity (0.01 == 1%).
    Evaluation order lives in :mod:`tradingagents.paper.risk`.
    """

    max_risk_per_trade_pct: float = Field(default=0.01, gt=0, le=1)
    max_daily_loss_pct: float = Field(default=0.03, gt=0, le=1)
    max_drawdown_pct: float = Field(default=0.20, gt=0, le=1)


class ScheduleEntry(BaseModel):
    """One asset/timeframe the scheduler may run research cycles for."""

    asset_id: str
    timeframe: str
    enabled: bool = True
    # Minutes to wait past a bar's effective close before the slot is due;
    # gives the data vendor time to publish the completed candle.
    offset_minutes: int = Field(default=1, ge=0, le=60)

    @field_validator("asset_id")
    @classmethod
    def _asset_must_exist(cls, value: str) -> str:
        try:
            return get_asset(value).asset_id
        except (UnknownAssetError, KeyError) as exc:
            raise ValueError(f"unknown asset id: {value!r}") from exc

    @field_validator("timeframe")
    @classmethod
    def _timeframe_must_exist(cls, value: str) -> str:
        try:
            return Timeframe(value.lower()).value
        except ValueError as exc:
            raise ValueError(
                f"unsupported timeframe {value!r}; "
                f"expected one of {[tf.value for tf in Timeframe]}"
            ) from exc


class PaperTradingConfig(BaseModel):
    """Full paper-trading configuration for one environment/account pair."""

    environment: Environment = ENV_TEST
    enabled: bool = False  # kill switch — must be armed explicitly
    account_id: str = DEFAULT_ACCOUNT_ID
    initial_capital: float = Field(default=DEFAULT_INITIAL_CAPITAL, gt=0)

    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    sizing: SizingPolicy = Field(default_factory=SizingPolicy)
    risk: PaperRiskLimits = Field(default_factory=PaperRiskLimits)

    # Optional per-timeframe staleness overrides in hours (tighter than the
    # Phase 1 defaults); keys are Timeframe values ("15m", "1h", "4h", "1d").
    stale_overrides_hours: dict[str, float] = Field(default_factory=dict)

    enable_macro: bool = False
    research_config: dict[str, Any] = Field(default_factory=dict)

    schedules: list[ScheduleEntry] = Field(default_factory=list)

    @field_validator("account_id")
    @classmethod
    def _account_id_is_path_safe(cls, value: str) -> str:
        if not _ACCOUNT_ID_RE.fullmatch(value):
            raise ValueError(
                "account_id must match ^[a-z0-9][a-z0-9_-]{0,63}$ "
                "(it is used as a directory name)"
            )
        return value

    @field_validator("stale_overrides_hours")
    @classmethod
    def _overrides_use_valid_timeframes(cls, value: dict[str, float]) -> dict[str, float]:
        valid = {tf.value for tf in Timeframe}
        for key, hours in value.items():
            if key not in valid:
                raise ValueError(f"stale override key {key!r} not in {sorted(valid)}")
            if hours <= 0:
                raise ValueError("stale overrides must be positive hours")
        return value

    def schedule_for(self, asset_id: str, timeframe: str) -> ScheduleEntry | None:
        """Centralized schedule lookup — agents never carry scheduling logic."""
        wanted_tf = Timeframe(timeframe.lower()).value
        wanted_asset = get_asset(asset_id).asset_id
        for entry in self.schedules:
            if (
                entry.enabled
                and get_asset(entry.asset_id).asset_id == wanted_asset
                and entry.timeframe == wanted_tf
            ):
                return entry
        return None


__all__ = [
    "ENV_PAPER",
    "ENV_TEST",
    "Environment",
    "PaperRiskLimits",
    "PaperTradingConfig",
    "ScheduleEntry",
]
