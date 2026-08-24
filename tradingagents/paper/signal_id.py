"""Deterministic, idempotent signal identity.

The signal id is a pure function of the market state that produced the
signal — the same composition as the Phase 2 research cache key
(:mod:`tradingagents.backtest.research_cache`), so identical market state
always yields the identical id no matter how many times the scheduler runs,
the process restarts, or retries occur. Duplicate protection is therefore
content-based, not session-based (ARCHITECTURE.md P3.4/P3.5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from tradingagents.backtest.research_cache import PROMPT_VERSION

SIGNAL_ID_LENGTH = 16


def utc_iso(moment: datetime) -> str:
    """UTC ISO-8601 rendering of ``moment`` (naive input rejected upstream)."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat()


def visible_bars_digest(bars: list[Any]) -> str:
    """Content hash over the exact bars visible at decision time."""
    from tradingagents.backtest.historical.store import bars_content_hash

    return bars_content_hash(bars)


def compute_config_hash(research_config: Mapping[str, Any]) -> str:
    """Stable short hash of the research configuration mapping."""
    payload = json.dumps(
        sorted((str(k), str(v)) for k, v in research_config.items()),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def compute_signal_id(
    *,
    asset_id: str,
    timeframe: str,
    decision_bar_close: datetime,
    visible_bars_hash: str,
    model_ids: list[str],
    config_hash: str,
) -> str:
    """Idempotent signal id: sha256 of every identity component, truncated.

    Any component change (new bar, different models, prompt/config version)
    produces a different id; repeating the *same* market state reproduces
    exactly the same id.
    """
    payload = "|".join(
        [
            asset_id.strip().upper(),
            timeframe.strip().lower(),
            utc_iso(decision_bar_close),
            visible_bars_hash,
            ",".join(sorted(model_ids)),
            PROMPT_VERSION,
            config_hash,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SIGNAL_ID_LENGTH]


__all__ = [
    "PROMPT_VERSION",
    "SIGNAL_ID_LENGTH",
    "compute_config_hash",
    "compute_signal_id",
    "utc_iso",
    "visible_bars_digest",
]
