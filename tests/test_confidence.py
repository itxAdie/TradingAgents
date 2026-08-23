"""Unit tests: experimental confidence heuristic."""

from __future__ import annotations

import pytest

from tradingagents.analysis import confidence as conf


@pytest.mark.unit
def test_weights_sum_to_one():
    total = (
        conf.WEIGHT_AGENT_AGREEMENT
        + conf.WEIGHT_DATA_COMPLETENESS
        + conf.WEIGHT_SIGNAL_CONSISTENCY
        + conf.WEIGHT_MODEL_CONFIDENCE
    )
    assert abs(total - 1.0) < 1e-9


@pytest.mark.unit
class TestAgentAgreement:
    def test_all_available_voters_agree_on_buy(self):
        frac, total, agreeing = conf.agent_agreement(
            "BUY",
            trend="up", momentum="bullish",
            macro_direction="supportive_bullish",
            news_tone="bullish", sentiment_band="Bullish",
            manager_direction="BUY",
        )
        assert (frac, total, agreeing) == (1.0, 6, 6)

    def test_missing_voters_shrink_denominator(self):
        frac, total, _ = conf.agent_agreement(
            "BUY", trend="up", momentum=None,
            macro_direction=None, news_tone=None,
            sentiment_band=None, manager_direction="BUY",
        )
        assert total == 2 and frac == 1.0

    def test_dissent_reduces_fraction(self):
        frac, _, _ = conf.agent_agreement(
            "BUY",
            trend="down", momentum="bearish",
            macro_direction=None, news_tone=None,
            sentiment_band=None, manager_direction="SELL",
        )
        assert frac == 0.0

    def test_no_voters_neutral(self):
        frac, total, _ = conf.agent_agreement(
            "BUY", trend=None, momentum=None, macro_direction=None,
            news_tone=None, sentiment_band=None, manager_direction=None,
        )
        assert (total, frac) == (0, 0.5)

    def test_hold_judged_by_neutral_votes(self):
        frac, total, agreeing = conf.agent_agreement(
            "HOLD",
            trend="sideways", momentum="neutral",
            macro_direction=None, news_tone="neutral",
            sentiment_band="Mixed", manager_direction=None,
        )
        assert total >= 4 and frac == pytest.approx(agreeing / total)


@pytest.mark.unit
class TestCompleteness:
    def test_fractions(self):
        f = conf.data_completeness
        assert f(news_available=True, sentiment_available=True, macro_available=True) == 1.0
        assert f(news_available=False, sentiment_available=False, macro_available=False) == 0.0
        assert f(news_available=True, sentiment_available=False, macro_available=False) == pytest.approx(1 / 3)


@pytest.mark.unit
class TestConsistency:
    def test_aligned_buy_full_score(self):
        assert conf.signal_consistency("BUY", trend="up", momentum="bullish") == 1.0
        assert conf.signal_consistency("SELL", trend="down", momentum="bearish") == 1.0

    def test_contradicted_action_penalized(self):
        assert conf.signal_consistency("BUY", trend="down", momentum="bearish") < 0.5

    def test_hold_prefers_unknown_regime(self):
        assert conf.signal_consistency("HOLD", trend=None, momentum=None) > \
               conf.signal_consistency("HOLD", trend="up", momentum="bullish")


@pytest.mark.unit
class TestComputeConfidence:
    BASE = {
        "trend": "up", "momentum": "bullish",
        "macro_direction": "supportive_bullish",
        "news_tone": "bullish", "sentiment_band": "Bullish",
        "manager_direction": "BUY",
        "news_available": True, "sentiment_available": True, "macro_available": True,
    }

    def _compute(self, **overrides):
        kwargs = {**self.BASE, **overrides}
        return conf.compute_confidence("BUY", **kwargs)

    def test_perfect_inputs_high_score(self):
        b = self._compute(
            news_available=True, sentiment_available=True, macro_available=True,
            model_confidences=[0.9],
        )
        assert 0.8 < b.score <= 1.0

    def test_components_move_score_monotonically(self):
        full = self._compute(news_available=True, sentiment_available=True, macro_available=True)
        fewer = self._compute(news_available=False, sentiment_available=False, macro_available=False)
        assert full.score > fewer.score

    def test_bounded_and_breakdown_present(self):
        b = self._compute(model_confidences=[2.0])  # out-of-range input clamped
        assert 0.0 <= b.score <= 1.0
        assert set(b.weights) == {"agent_agreement", "data_completeness",
                                  "signal_consistency", "model_confidence"}
        assert abs(sum(b.weights.values()) - 1.0) < 1e-9

    def test_absent_model_confidence_is_neutral(self):
        b = self._compute()
        assert b.model_confidence == 0.5
