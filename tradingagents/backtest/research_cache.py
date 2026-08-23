"""Research-signal reuse cache for LLM-driven backtests.

Re-running the research engine at every historical bar would make AI
backtests prohibitively slow and expensive. When everything that determines
a decision is identical, the previously generated research output may be
reused (spec §"Computational efficiency").

Cache key = SHA-256 over:
    asset_id | timeframe | decision timestamp | bars-content-hash(up to T)
    | model ids | prompt_version | research-config hash

Invalidation: ANY key component changes ⇒ new key (no partial invalidation,
no TTL — content-addressed keys are self-invalidating). Hits are recorded in
the backtest report; reproducibility is preserved because a hit implies
byte-identical inputs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tradingagents.marketdata.models import OhlcvSeries
from tradingagents.research.schemas import ResearchSignal

# Bump when prompts/agent logic change so old cache entries can never leak
# into new methodology runs. Recorded in reports for provenance.
PROMPT_VERSION = "phase1-2026-08-23"


class ResearchCache:
    """File-backed store of generated signals keyed by full input identity."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(
        *,
        asset_id: str,
        timeframe: str,
        decision_at: str,
        visible_bars_hash: str,
        model_ids: list[str],
        config_hash: str,
    ) -> str:
        identity = "|".join((
            asset_id, timeframe, decision_at, visible_bars_hash,
            ",".join(sorted(model_ids)), PROMPT_VERSION, config_hash,
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def get(
        self,
        *,
        asset_id: str,
        timeframe: str,
        decision_at: str,
        visible: OhlcvSeries,
        model_ids: list[str],
        config_hash: str,
    ) -> ResearchSignal | None:
        from tradingagents.backtest.historical.store import bars_content_hash

        key = self._key(
            asset_id=asset_id, timeframe=timeframe, decision_at=decision_at,
            visible_bars_hash=bars_content_hash(visible.bars),
            model_ids=model_ids, config_hash=config_hash,
        )
        path = self.root / f"{key}.json"
        if not path.exists():
            self.misses += 1
            return None
        try:
            signal = ResearchSignal(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - corrupt entry == miss, never fatal
            self.misses += 1
            return None
        self.hits += 1
        return signal

    def put(
        self,
        signal: ResearchSignal,
        *,
        decision_at: str,
        visible: OhlcvSeries,
        model_ids: list[str],
        config_hash: str,
    ) -> None:
        from tradingagents.backtest.historical.store import bars_content_hash

        key = self._key(
            asset_id=signal.asset_id, timeframe=signal.timeframe,
            decision_at=decision_at,
            visible_bars_hash=bars_content_hash(visible.bars),
            model_ids=model_ids, config_hash=config_hash,
        )
        path = self.root / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(signal.model_dump_json(), encoding="utf-8")
        tmp.replace(path)


__all__ = ["PROMPT_VERSION", "ResearchCache"]
