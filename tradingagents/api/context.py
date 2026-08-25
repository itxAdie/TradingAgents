"""Shared server context: stores, engines, bus, registries.

One :class:`AppContext` instance is attached to the FastAPI app state at
startup and handed to route handlers via dependencies. Everything is
constructed lazily so tests can inject fakes and the CLI can boot instantly.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.api.audit import AuditLog
from tradingagents.api.backtests import BacktestRunRegistry
from tradingagents.api.eventbus import EventBus
from tradingagents.api.researchstore import PersistingResearchRunner, ResearchArtifactStore
from tradingagents.paper.config import PaperTradingConfig, ScheduleEntry
from tradingagents.paper.engine import PaperTradingEngine
from tradingagents.paper.store import JsonPaperStateStore


class AppContext:
    """Dependency root for all routes; one per server process."""

    def __init__(self, settings):  # settings: ServerSettings (avoids import cycle)
        from tradingagents.default_config import DEFAULT_CONFIG

        self.settings = settings
        self.cache_dir = Path(str(DEFAULT_CONFIG["data_cache_dir"]))
        self.paper_root = self.cache_dir / "paper"
        self.bus = EventBus()
        self.audit = AuditLog(self.cache_dir / "audit.jsonl", self.bus)
        self.research_store = ResearchArtifactStore(self.cache_dir / "research_runs")
        self.backtests = BacktestRunRegistry(
            self.cache_dir / "backtests", bus=self.bus, audit=self.audit
        )
        self.last_quote_poll: dict[str, dict] = {}
        self.quote_poll_failures = 0
        self._loop_thread: threading.Thread | None = None
        self._quote_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paper_configs: dict[tuple[str, str], PaperTradingConfig] = {}
        self._broker_engine = None  # lazy singleton; startup state must persist
        self._broker_lock = threading.Lock()

    # -- configuration ------------------------------------------------------------

    def _paper_config(self, environment: str, account_id: str) -> PaperTradingConfig:
        """Server-managed config for an account; schedules come from serve flags."""
        key = (environment, account_id)
        cached = self._paper_configs.get(key)
        if cached is None:
            cached = PaperTradingConfig(environment=environment, account_id=account_id)
            self._paper_configs[key] = cached
        return cached

    def server_config(self) -> PaperTradingConfig:
        """The armed/disarmed paper config this server was launched with."""
        schedules = [
            ScheduleEntry(asset_id=a, timeframe=t)
            for a in self.settings.assets
            for t in self.settings.timeframes
        ]
        return PaperTradingConfig(
            environment=self.settings.environment,
            account_id=self.settings.account_id,
            enabled=self.settings.enable_research_loop,
            schedules=schedules,
        )

    # -- stores & engines ------------------------------------------------------------

    def store(self, environment: str | None = None, account: str | None = None):
        return JsonPaperStateStore(
            self.paper_root,
            environment=environment or self.settings.environment,
            account_id=account or self.settings.account_id,
        )

    def engine(
        self,
        environment: str | None = None,
        account: str | None = None,
        *,
        config: PaperTradingConfig | None = None,
    ) -> PaperTradingEngine:
        cfg = config or self._paper_config(
            environment or self.settings.environment,
            account or self.settings.account_id,
        )
        return PaperTradingEngine(
            config=cfg,
            store=self.store(cfg.environment, cfg.account_id),
            provider=self.provider(),
            runner=self.research_runner(),
            notifier=self.bus,
        )

    def provider(self):
        if getattr(self, "_provider", None) is None:
            from tradingagents.marketdata.yahoo_provider import YahooMarketDataProvider

            self._provider = YahooMarketDataProvider()
        return self._provider

    def research_runner(self) -> PersistingResearchRunner:
        if getattr(self, "_runner", None) is None:
            self._runner = PersistingResearchRunner(
                artifact_store=self.research_store,
                bus=self.bus,
                provider=self.provider(),
                now_fn=lambda: datetime.now(timezone.utc),
            )
        return self._runner

    def broker_engine(self):
        """Process-wide LiveExecutionEngine (sandbox adapter for now).

        A singleton on purpose: ``started/ready`` and the consecutive-loss /
        day-anchor state live in memory, so per-request construction would
        silently reset the safety posture while the store looked fine.
        """
        if self._broker_engine is not None:
            return self._broker_engine
        with self._broker_lock:
            if self._broker_engine is not None:
                return self._broker_engine
            from tradingagents.brokers.registry import build_broker
            from tradingagents.execution.config import load_live_execution_config
            from tradingagents.execution.engine import LiveExecutionEngine

            config = load_live_execution_config(
                broker_name=self.settings.broker_name,
                cache_dir=self.cache_dir,
                account_id=None,  # env-provided / default sandbox account
            )
            adapter = build_broker(
                config.broker_name,
                account_id=config.account_id,
                base_currency=config.base_currency,
            )
            self._broker_engine = LiveExecutionEngine(
                config=config,
                adapter=adapter,
                provider=self.provider(),
                notifier=self.bus,
                bus=self.bus,
                audit=self.audit.record,
            )
            return self._broker_engine

    # -- background workers ---------------------------------------------------------

    def start_background_workers(self) -> None:
        if self.settings.enable_research_loop:
            self.audit.record("research_loop_armed", **self.server_config_safety_snapshot())
            self._loop_thread = threading.Thread(
                target=self._research_loop, name="paper-loop", daemon=True
            )
            self._loop_thread.start()
        if self.settings.quote_poll_seconds > 0:
            self._quote_thread = threading.Thread(
                target=self._quote_loop, name="quote-poll", daemon=True
            )
            self._quote_thread.start()

    def shutdown_background_workers(self) -> None:
        self._stop.set()

    def server_config_safety_snapshot(self) -> dict:
        cfg = self.server_config()
        return {
            "environment": cfg.environment,
            "account_id": cfg.account_id,
            "enabled": cfg.enabled,
            "schedules": [f"{s.asset_id}:{s.timeframe}" for s in cfg.schedules],
        }

    def _research_loop(self) -> None:
        """Same cadence as `paper run --loop`; engine unchanged."""
        cfg = self.server_config()
        engine = self.engine(config=cfg)
        while not self._stop.is_set():
            for entry in cfg.schedules:
                if self._stop.is_set():
                    return
                try:
                    engine.run_cycle(entry.asset_id, entry.timeframe)
                except Exception as exc:
                    self.bus.notify(
                        "cycle_failed",
                        {
                            "asset_id": entry.asset_id,
                            "timeframe": entry.timeframe,
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:500],
                        },
                    )
            self._stop.wait(self._seconds_until_next(engine, cfg))

    @staticmethod
    def _seconds_until_next(engine: PaperTradingEngine, cfg: PaperTradingConfig) -> float:
        from tradingagents.marketdata.timeframes import Timeframe
        from tradingagents.paper.scheduler import ScheduleKey

        now = datetime.now(timezone.utc)
        delays: list[float] = []
        for entry in cfg.schedules:
            key = ScheduleKey(entry.asset_id.upper(), Timeframe(entry.timeframe).value)
            state = engine.store.load_schedule_state(key.value()) or {}
            raw = state.get("next_run_at")
            if raw:
                next_run = datetime.fromisoformat(raw)
                if next_run > now:
                    delays.append((next_run - now).total_seconds())
        return max(5.0, min(min(delays) if delays else 60.0, 900.0))

    def _quote_loop(self) -> None:
        """Conservative price polling → PRICE_UPDATED events (spec §7/§29)."""
        interval = max(10, self.settings.quote_poll_seconds)
        # Wait for an initial settle; avoid hammering on startup bursts.
        time.sleep(2.0)
        while not self._stop.is_set():
            for asset_id in self.settings.assets:
                if self._stop.is_set():
                    return
                try:
                    from tradingagents.assets.registry import get_asset

                    quote = self.provider().get_quote(get_asset(asset_id))
                    if quote is not None:
                        self.last_quote_poll[asset_id] = {
                            "asset_id": asset_id,
                            "ts": quote.timestamp.isoformat(),
                            "last": quote.last,
                            "source": quote.source,
                            "data_status": (
                                quote.status.value
                                if hasattr(quote.status, "value")
                                else str(quote.status)
                            ),
                        }
                        self.bus.notify(
                            "price_updated",
                            {
                                "asset_id": asset_id,
                                "last": quote.last,
                                "ts": quote.timestamp.isoformat(),
                                "source": quote.source,
                                "data_status": self.last_quote_poll[asset_id]["data_status"],
                            },
                        )
                except Exception:
                    self.quote_poll_failures += 1
            self._stop.wait(interval)


__all__ = ["AppContext"]
