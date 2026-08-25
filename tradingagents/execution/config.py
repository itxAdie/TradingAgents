"""Execution configuration: environment separation + hard risk limits (P5).

SAFETY MODEL
- ``Environment`` extends the paper world with ``demo`` and ``live`` but the
  paper package is untouched (its config remains ``Literal["test","paper"]``).
- A ``LiveExecutionConfig`` for ``environment="live"`` cannot even be
  constructed unless BOTH ``LIVE_TRADING_ENABLED=true`` AND
  ``BROKER_ENVIRONMENT=live`` AND an explicit operator activation record is
  supplied. There is no default path that yields a live-capable config.
- Risk limits are deterministic configuration, content-versioned; the AI can
  never modify them (it has no write path — enforced structurally by import
  direction tests).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tradingagents.backtest.config import RiskLimits

ENV_TEST = "test"
ENV_PAPER = "paper"
ENV_DEMO = "demo"
ENV_LIVE = "live"

#: Full execution-world environment set. Paper keeps its own narrower literal.
ExecutionEnvironment = Literal["test", "paper", "demo", "live"]

LIVE_TRADING_ENABLED_ENV_VAR = "LIVE_TRADING_ENABLED"
BROKER_ENVIRONMENT_ENV_VAR = "BROKER_ENVIRONMENT"


class LiveRiskLimits(RiskLimits):
    """Hard caps for real execution. Superset of the simulated limits.

    UNIT CONVENTION: every ``*_pct`` field in THIS model is percent of equity
    (``2.0`` == 2%). This intentionally differs from ``paper.config.
    PaperRiskLimits``, whose fraction-valued fields use 0.01 == 1% — the two
    models must never be mixed; tests pin the distinction.

    All fields are deterministic configuration; nothing here is writable by
    research/AI code paths. Financial values are operator decisions — the
    defaults exist only so validation has something to check and are
    deliberately conservative.
    """

    max_risk_per_trade_pct: float = Field(default=0.5, gt=0, le=100)
    max_daily_loss_pct: float = Field(default=2.0, gt=0, le=100)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    max_leverage: float = Field(default=1.0, ge=1)  # system cap; broker cap never used
    max_order_value: float = Field(default=25_000.0, gt=0)
    max_open_orders: int = Field(default=5, ge=1)
    max_consecutive_losses: int = Field(default=3, ge=1)


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean for {name}: {raw!r}")


def broker_environment_from_env() -> str:
    raw = os.environ.get(BROKER_ENVIRONMENT_ENV_VAR, "").strip().lower()
    if not raw:
        return ENV_DEMO
    if raw not in {ENV_TEST, ENV_PAPER, ENV_DEMO, ENV_LIVE}:
        raise ValueError(f"invalid {BROKER_ENVIRONMENT_ENV_VAR}: {raw!r}")
    return raw


class ActivationRecord(BaseModel):
    """One deliberate operator activation of an execution environment."""

    environment: str
    account_id: str
    activated_at: datetime
    operator: str  # free-text identity; never a credential
    confirmation_phrase: Literal["ACTIVATE"]  # typed phrase required
    checklist_passed: bool


class LiveExecutionConfig(BaseModel):
    """Configuration for one broker/account/environment triple.

    Construction rules (fail-closed):
    - ``environment="live"`` requires ``live_enabled=True`` which itself
      requires the env flag AND env environment to both say live.
    - ``activation`` must be provided exactly when environment == live.
    """

    broker_name: str  # adapter registry key; no default broker exists
    environment: ExecutionEnvironment
    account_id: str
    cache_dir: Path
    base_currency: str = "USD"
    data_staleness_override: dict[str, int] | None = None
    poll_interval_seconds: float = Field(default=5.0, ge=1)
    reconciliation_interval_seconds: float = Field(default=60.0, ge=5)
    rate_limit_per_second: float = Field(default=5.0, gt=0)
    #: |fill slippage| above this many bps triggers EXCESSIVE_SLIPPAGE
    max_slippage_bps: float = Field(default=50.0, ge=0)
    limits: LiveRiskLimits = LiveRiskLimits()
    activation: ActivationRecord | None = None

    def model_post_init(self, __context: object) -> None:
        if self.broker_name == "sandbox" and self.environment == ENV_LIVE:
            # the simulator must never carry real orders even if every env
            # flag is misconfigured — live requires a real venue adapter
            raise ValueError(
                "live execution config refused: broker_name='sandbox' cannot "
                "be armed for environment='live'"
            )
        if self.environment == ENV_LIVE:
            env_flag = _env_flag(LIVE_TRADING_ENABLED_ENV_VAR)
            env_env = broker_environment_from_env()
            if not env_flag or env_env != ENV_LIVE:
                raise ValueError(
                    "live execution config refused: requires "
                    f"{LIVE_TRADING_ENABLED_ENV_VAR}=true AND "
                    f"{BROKER_ENVIRONMENT_ENV_VAR}=live "
                    f"(got flag={env_flag}, env={env_env!r})"
                )
            if self.activation is None or not self.activation.checklist_passed:
                raise ValueError(
                    "live execution config refused: explicit operator activation "
                    "record with passed startup checklist is required"
                )
        elif self.activation is not None:
            raise ValueError("activation records are only valid for environment=live")

    @property
    def live_armed(self) -> bool:
        """True only when real orders are possible at all."""
        return self.environment == ENV_LIVE and self.activation is not None

    def store_root(self) -> Path:
        return self.cache_dir / "live" / self.environment / self.account_id

    def configuration_version(self) -> str:
        payload = {
            "broker": self.broker_name,
            "environment": self.environment,
            "account": self.account_id,
            "limits": json.loads(self.limits.model_dump_json()),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def load_live_execution_config(
    *,
    broker_name: str,
    cache_dir: Path | None = None,
    account_id: str | None = None,
) -> LiveExecutionConfig:
    """Build the config from environment variables; demo unless told otherwise.

    Never reads or returns secrets — credentials stay inside the adapter,
    fetched from the environment at connect time.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    resolved_cache = Path(
        cache_dir if cache_dir is not None else DEFAULT_CONFIG.get("data_cache_dir", ".cache")
    )
    env = broker_environment_from_env()
    live_enabled = _env_flag(LIVE_TRADING_ENABLED_ENV_VAR)
    if env == ENV_LIVE and not live_enabled:
        # fail closed: refuse to build a live config silently downgraded to demo
        raise ValueError(
            f"{BROKER_ENVIRONMENT_ENV_VAR}=live but {LIVE_TRADING_ENABLED_ENV_VAR} "
            "is false — refusing ambiguous configuration"
        )
    return LiveExecutionConfig(
        broker_name=broker_name,
        environment=env,  # type: ignore[arg-type]
        account_id=account_id or os.environ.get("BROKER_ACCOUNT_ID", f"ta-{env}"),
        cache_dir=resolved_cache,
    )
