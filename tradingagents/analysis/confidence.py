"""Experimental confidence heuristic for research signals.

IMPORTANT: This is a **clearly labelled experimental heuristic**, not a
statistically validated measure (PROJECT_RULES §5, brief §CONFIDENCE). It
aggregates four documented components into [0,1]; every component degrades to
a neutral default when its inputs are unavailable, and the breakdown is always
attached to the result so consumers can see *why* a number looks the way it
does.

Components and weights (sum to 1.0):

- ``agent_agreement`` (0.40): fraction of available directional voters that
  agree with the final action. Voters: technical classification (trend +
  momentum), macro analysis direction (if available), news tone (if
  available), sentiment band (if available), bull case strength vs bear case,
  research-manager direction.
- ``data_completeness`` (0.25): fraction of expected sources actually
  available (market data mandatory and excluded from the fraction; news,
  sentiment, macro optional).
- ``signal_consistency`` (0.20): agreement between the final action and the
  deterministic technical regime (e.g., BUY while price is below SMA50 with
  falling momentum reduces this).
- ``model_confidence`` (0.15): mean of self-reported model confidences where
  provided (normalized to [0,1]); neutral 0.5 when absent.
"""

from __future__ import annotations

from dataclasses import dataclass

# Documented weights — changing them changes documented methodology.
WEIGHT_AGENT_AGREEMENT = 0.40
WEIGHT_DATA_COMPLETENESS = 0.25
WEIGHT_SIGNAL_CONSISTENCY = 0.20
WEIGHT_MODEL_CONFIDENCE = 0.15

_NEUTRAL = 0.5


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """Full audit trail of one confidence computation."""

    agent_agreement: float
    data_completeness: float
    signal_consistency: float
    model_confidence: float
    weights: dict[str, float]
    score: float
    voters_total: int
    voters_agreeing: int


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def agent_agreement(
    action: str,
    *,
    trend: str | None,
    momentum: str | None,
    macro_direction: str | None,
    news_tone: str | None,
    sentiment_band: str | None,
    manager_direction: str | None,
) -> tuple[float, int, int]:
    """Fraction of available directional voters agreeing with ``action``.

    Returns ``(fraction, total_voters, agreeing_voters)``; neutral verdicts
    count as voters but neither agree nor disagree.
    """
    def vote(directional: str | None) -> str | None:
        if not directional or directional in {"neutral", "mixed", "unknown"}:
            return None if directional is None else "neutral"
        return directional

    bullish = {"up", "bullish", "buy", "bull", "positive"}
    bearish = {"down", "bearish", "sell", "bear", "negative"}

    def side(term: str | None) -> str | None:
        v = vote(term)
        if v in (None, "neutral"):
            return "neutral" if v == "neutral" else None
        t = v.lower()
        if t in bullish:
            return "BUY"
        if t in bearish:
            return "SELL"
        # Composite vocabularies: macro directions ("supportive_bullish")
        # and sentiment bands ("Mildly Bearish") map by substring.
        if "bullish" in t:
            return "BUY"
        if "bearish" in t:
            return "SELL"
        return "neutral"

    voters: list[str | None] = [
        side(trend),
        side(momentum),
        side(macro_direction),
        side(news_tone),
        side(sentiment_band),
        side(manager_direction) if action in {"BUY", "SELL"} else None,
    ]
    # A HOLD action cannot be "agreed" with by directional votes except by
    # neutrals; treat only explicit neutral signals as agreement.
    total = sum(1 for v in voters if v is not None)
    if total == 0:
        return _NEUTRAL, 0, 0
    if action == "HOLD":
        agreeing = sum(1 for v in voters if v == "neutral")
    else:
        agreeing = sum(1 for v in voters if v == action)
    return agreeing / total, total, agreeing


def data_completeness(
    *, news_available: bool, sentiment_available: bool, macro_available: bool
) -> float:
    """Fraction of optional enrichment sources present (market data excluded)."""
    flags = [news_available, sentiment_available, macro_available]
    return sum(1 for f in flags if f) / len(flags)


def signal_consistency(action: str, *, trend: str | None, momentum: str | None) -> float:
    """Agreement of the action with the deterministic technical regime."""
    if action == "HOLD":
        return 1.0 if trend in {None, "sideways", "unknown"} else 0.6
    expected = {
        ("BUY", "up"): 1.0,
        ("SELL", "down"): 1.0,
    }
    base = expected.get((action, trend), 0.4 if trend not in {None, "unknown"} else _NEUTRAL)
    mom_ok = momentum in {None, "unknown"} or (
        (action == "BUY" and momentum == "bullish")
        or (action == "SELL" and momentum == "bearish")
    )
    return _clamp01(base if mom_ok else base * 0.6)


def compute_confidence(
    action: str,
    *,
    trend: str | None,
    momentum: str | None,
    macro_direction: str | None,
    news_tone: str | None,
    sentiment_band: str | None,
    manager_direction: str | None,
    news_available: bool,
    sentiment_available: bool,
    macro_available: bool,
    model_confidences: list[float] | None = None,
) -> ConfidenceBreakdown:
    """Aggregate the documented components into an experimental [0,1] score."""
    agreement, total, agreeing = agent_agreement(
        action,
        trend=trend,
        momentum=momentum,
        macro_direction=macro_direction,
        news_tone=news_tone,
        sentiment_band=sentiment_band,
        manager_direction=manager_direction,
    )
    completeness = data_completeness(
        news_available=news_available,
        sentiment_available=sentiment_available,
        macro_available=macro_available,
    )
    consistency = signal_consistency(action, trend=trend, momentum=momentum)

    self_reports = [c for c in (model_confidences or []) if c is not None]
    model_component = (
        _clamp01(sum(self_reports) / len(self_reports)) if self_reports else _NEUTRAL
    )

    score = (
        WEIGHT_AGENT_AGREEMENT * agreement
        + WEIGHT_DATA_COMPLETENESS * completeness
        + WEIGHT_SIGNAL_CONSISTENCY * consistency
        + WEIGHT_MODEL_CONFIDENCE * model_component
    )
    return ConfidenceBreakdown(
        agent_agreement=round(agreement, 4),
        data_completeness=round(completeness, 4),
        signal_consistency=round(consistency, 4),
        model_confidence=round(model_component, 4),
        weights={
            "agent_agreement": WEIGHT_AGENT_AGREEMENT,
            "data_completeness": WEIGHT_DATA_COMPLETENESS,
            "signal_consistency": WEIGHT_SIGNAL_CONSISTENCY,
            "model_confidence": WEIGHT_MODEL_CONFIDENCE,
        },
        score=round(_clamp01(score), 4),
        voters_total=total,
        voters_agreeing=agreeing,
    )
