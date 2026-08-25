"""ExecutionPolicy: the fail-closed pre-flight chain before any submission.

Every check returns a decision; every failure blocks. Unknown inputs block
(§42): stale data, disconnected broker, account-identity mismatch,
unresolved reconciliation mismatches, tripped breaker, operator halt,
disabled/unactivated environment, unwritable store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from tradingagents.brokers.base import BrokerAccountSnapshot, ConnectionStatus
from tradingagents.execution.config import ENV_LIVE, LiveExecutionConfig
from tradingagents.execution.store import ExecutionStore, ExecutionStoreError
from tradingagents.marketdata.timeframes import Timeframe


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason_code: str = ""
    detail: str = ""

    @classmethod
    def ok(cls) -> PolicyDecision:
        return cls(allowed=True)

    @classmethod
    def block(cls, code: str, detail: str) -> PolicyDecision:
        return cls(allowed=False, reason_code=code, detail=detail)


class ExecutionPolicy:
    def __init__(
        self,
        *,
        config: LiveExecutionConfig,
        store: ExecutionStore,
    ) -> None:
        self._config = config
        self._store = store

    def check(
        self,
        *,
        connection: ConnectionStatus,
        adapter_healthy: bool,
        account: BrokerAccountSnapshot | None,
        last_bar_close: datetime | None,
        timeframe: str,
        now: datetime | None = None,
        store_writable: bool | None = None,
    ) -> PolicyDecision:
        try:
            return self._ordered(
                connection=connection,
                adapter_healthy=adapter_healthy,
                account=account,
                last_bar_close=last_bar_close,
                timeframe=timeframe,
                now=now or datetime.now(timezone.utc),
                store_writable=store_writable if store_writable is not None else True,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on ANY internal error
            return PolicyDecision.block("policy_error", f"execution policy internal failure: {exc}")

    def _ordered(
        self,
        *,
        connection: ConnectionStatus,
        adapter_healthy: bool,
        account: BrokerAccountSnapshot | None,
        last_bar_close: datetime | None,
        timeframe: str,
        now: datetime,
        store_writable: bool,
    ) -> PolicyDecision:
        cfg = self._config

        if cfg.environment == ENV_LIVE and not cfg.live_armed:
            return PolicyDecision.block("live_not_activated", "environment=live without activation")

        halted, halt_reason = self._store.is_halted()
        if halted:
            return PolicyDecision.block("trading_halted", halt_reason or "halted")

        tripped, trip_reason = self._store.circuit_breaker_state()
        if tripped:
            return PolicyDecision.block("circuit_breaker", trip_reason or "breaker tripped")

        if not store_writable:
            return PolicyDecision.block("store_unavailable", "cannot persist orders — fail closed")
        try:
            self._store.is_halted()
        except ExecutionStoreError as exc:
            return PolicyDecision.block("store_corrupt", str(exc))

        if connection is not ConnectionStatus.CONNECTED:
            return PolicyDecision.block("broker_not_connected", f"connection={connection.value}")
        if not adapter_healthy:
            return PolicyDecision.block("broker_degraded", "health check failed")

        if account is None:
            return PolicyDecision.block("account_unavailable", "no account snapshot")
        if account.account_id != cfg.account_id:
            return PolicyDecision.block(
                "account_identity_mismatch",
                f"configured {cfg.account_id!r} but broker reports {account.account_id!r}",
            )
        if account.equity <= 0:
            return PolicyDecision.block("account_unhealthy", f"equity={account.equity}")

        report = self._store.last_reconciliation()
        if report is None:
            return PolicyDecision.block(
                "reconciliation_required",
                "no reconciliation has run yet in this store",
            )
        if not report.clean:
            return PolicyDecision.block(
                "reconciliation_incomplete",
                f"{len(report.mismatches)} unresolved mismatch(es)",
            )

        if last_bar_close is None:
            return PolicyDecision.block("market_data_missing", "no closed bar available")
        age_seconds = (now - last_bar_close).total_seconds()
        window_seconds = Timeframe(timeframe).staleness_hours() * 3600
        if age_seconds > window_seconds:
            return PolicyDecision.block(
                "stale_market_data",
                f"last close {age_seconds:.0f}s ago > freshness window {window_seconds:.0f}s",
            )

        return PolicyDecision.ok()
