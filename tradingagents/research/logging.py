"""Structured run logging for the research platform.

Emits one JSON line per event on logger ``tradingagents.research`` so a run
can be reconstructed after the fact: start (asset, timeframe), data fetch
(source, status, timestamps), per-agent completion/failure, model used, and
final signal. Secrets are never passed here and never logged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

LOGGER_NAME = "tradingagents.research"

# Keys that must never be forwarded even if a caller mistakenly passes them.
_FORBIDDEN_KEYS = {
    "api_key", "apikey", "token", "secret", "password", "authorization",
    "openai_api_key", "anthropic_api_key", "google_api_key", "fred_api_key",
}


def get_research_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(event: str, logger: logging.Logger | None = None, **fields: Any) -> None:
    """Emit a structured JSON event line.

    Non-serializable values fall back to ``str()``; ``datetime`` values are
    ISO-formatted; forbidden keys are dropped defensively.
    """
    lg = logger or get_research_logger()
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    for key, value in fields.items():
        if key.lower() in _FORBIDDEN_KEYS:
            continue
        if isinstance(value, datetime):
            value = value.isoformat()
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            value = str(value)
        payload[key] = value
    lg.info(json.dumps(payload, ensure_ascii=False))
