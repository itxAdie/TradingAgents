#!/usr/bin/env python
"""E2E fixture server: seeded paper account + deterministic fake provider.

Runs the real FastAPI app (same code path as `tradingagents serve`) against a
throwaway cache directory so Playwright exercises genuine endpoints with
offline-deterministic market data. Not used in production anywhere.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))


def build_synthetic_dataset(cache_dir: Path) -> None:
    """Store 520 hourly bars per asset so backtests can run end-to-end."""
    from tests.api_helpers import T0
    from tradingagents.backtest.historical.store import JsonDataStore, build_meta
    from tradingagents.marketdata.models import Bar, DataStatus, OhlcvSeries
    from tradingagents.marketdata.timeframes import Timeframe

    # the backtest worker resolves datasets under <cache>/historical
    store = JsonDataStore(cache_dir / "historical")
    for asset_id, base_price in (("XAUUSD", 2000.0), ("BTCUSD", 60_000.0)):
        bars = []
        price = base_price
        step = base_price * 0.0003
        for i in range(520):
            ts = T0 - timedelta(hours=520 - i)
            drift = step if (i // 12) % 2 == 0 else -step * 0.7
            price += drift
            bars.append(
                Bar(
                    timestamp=ts,
                    open=price,
                    high=price + step * 2.5,
                    low=price - step * 2.5,
                    close=price + step * 0.8,
                    volume=1000.0,
                )
            )
        series = OhlcvSeries(
            asset_id=asset_id,
            timeframe=Timeframe("1h"),
            bars=bars,
            source="e2e-fixture",
            status=DataStatus.SIMULATED,
        )
        store.save(series, build_meta(series, provider_symbol=asset_id))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8931)
    args = parser.parse_args()

    cache_dir = Path(tempfile.mkdtemp(prefix="ta-e2e-"))
    os.environ["TRADINGAGENTS_CACHE_DIR"] = str(cache_dir)

    # the backtest worker resolves datasets via DEFAULT_CONFIG, not the env var
    from tradingagents.default_config import DEFAULT_CONFIG

    DEFAULT_CONFIG["data_cache_dir"] = str(cache_dir)

    from tests.api_helpers import FakeProvider, seed_account
    from tradingagents.paper.store import JsonPaperStateStore

    store = JsonPaperStateStore(
        cache_dir / "paper", environment="test", account_id="paper-default"
    )
    seed_account(store)
    build_synthetic_dataset(cache_dir)

    from tradingagents.api.app import ServerSettings, create_app

    settings = ServerSettings(
        environment="test",
        account_id="paper-default",
        dashboard_dir=REPO / "dashboard" / "dist",  # serve the built SPA
    )
    app = create_app(settings)

    # Deterministic offline quotes/candles for the browser session.
    # provider() caches into ctx._provider, so pre-seeding it wins.
    ctx = app.state.ctx
    ctx._provider = FakeProvider()  # noqa: SLF001

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
