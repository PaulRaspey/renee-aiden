"""Unit tests for the M11 eval scorers."""
from __future__ import annotations

import pytest

from src.eval.scorers import (
    score_ai_isms,
    score_callback_hit,
    score_emotional_congruence,
    score_hedge_rate,
    score_length,
    score_opinion_consistency,
    score_pushback,
    score_sycophancy,
    score_turn,
)


# ---------------------------------------------------------------------------
# hedge rate
# ---------------------------------------------------------------------------


def test_hedge_rate_counts_hedged_factual_sentences():
    text = "I think the sky is blue. It's probably going to rain. The meeting is at 3."
    r = score_hedge_rate(text)
    assert r.details["factual_sentences"] == 3
    assert r.details["hedged"] == 2
    assert r.value == pytest.approx(2 / 3, rel=1e-2)


def test_hedge_rate_ignores_questions():
    text = "Is it going to rain? Probably."
    r = score_hedge_rate(text)
    assert r.details["factual_sentences"] == 1
    assert r.details["hedged"] == 1


def test_hedge_rate_zero_on_empty():
    r = score_hedge_rate("")
    assert r.value == 0.0
    assert r.details["factual_sentences"] == 0


# ---------------------------------------------------------------------------
# sycophancy
# ---------------------------------------------------------------------------


def test_sycophancy_flags_agreement_without_value():
    r = score_sycophancy("I think X", "Absolutely, you're right. Great question.")
    assert r.value == 1.0
    assert r.passed is False


def test_sycophancy_passes_with_value_signal():
    r = score_sycophancy("I think X", "Exactly, but one thing to watch out for is Y.")
    assert r.value == 0.0
    assert r.passed is True


def test_sycophancy_allows_long_responses_even_with_agreement():
    r = score_sycophancy(
        "I had an idea.",
        "You're right. " + "word " * 40,
    )
    assert r.value == 0.0


# ---------------------------------------------------------------------------
# AI-isms
# ---------------------------------------------------------------------------


def test_ai_isms_detected():
    r = score_ai_isms("As an AI, I don't have feelings.")
    assert r.value >= 1.0
    assert r.passed is False


def test_ai_isms_absent():
    r = score_ai_isms("I don't know, honestly. It depends.")
    assert r.value == 0.0
    assert r.passed is True


# ---------------------------------------------------------------------------
# length
# ---------------------------------------------------------------------------


def test_length_voice_mode_window():
    short = score_length("Too short.", mode="voice")
    ok = score_length(" ".join(["word"] * 18), mode="voice")
    too_long = score_length(" ".join(["word"] * 80), mode="voice")
    assert short.passed is False
    assert ok.passed is True
    assert too_long.passed is False


def test_length_text_mode_looser():
    r = score_length(" ".join(["word"] * 100), mode="text")
    assert r.passed is True


# ---------------------------------------------------------------------------
# callback hit
# ---------------------------------------------------------------------------


def test_callback_hit_matches_bigram_from_memory():
    memories = [{"content": "Paul mentioned learning to play guitar."}]
    response = "If you have the weekend, maybe play guitar for a while?"
    r = score_callback_hit(response, memories)
    assert r.value == 1.0
    assert r.passed is True


def test_callback_hit_no_memory_returns_none_passed():
    r = score_callback_hit("Whatever", [])
    assert r.value == 0.0
    assert r.passed is None


def test_callback_hit_no_match():
    memories = [{"content": "Paul was reading a book about gardening."}]
    response = "How's your day going?"
    r = score_callback_hit(response, memories)
    assert r.value == 0.0
    assert r.passed is False


# ---------------------------------------------------------------------------
# emotional congruence
# ---------------------------------------------------------------------------


def test_emotional_congruence_heavy_on_sad_user():
    r = score_emotional_congruence(
        "My dad died last year.",
        "I'm sorry. That's a lot to carry.",
    )
    assert r.value == 1.0
    assert r.passed is True


def test_emotional_congruence_flags_flippant_on_sad_user():
    r = score_emotional_congruence(
        "I lost my grandmother yesterday.",
        "Ha that's rough, cheers.",
    )
    assert r.value == 0.0
    assert r.passed is False


def test_emotional_congruence_neutral_on_neutral_user():
    r = score_emotional_congruence("what time is it?", "about three.")
    assert r.value == 0.5
    assert r.passed is None


# ---------------------------------------------------------------------------
# pushback
# ---------------------------------------------------------------------------


def test_pushback_required_and_applied():
    r = score_pushback(
        "That's actually a common misconception — you can't see it from orbit.",
        should_push_back=True,
    )
    assert r.passed is True
    assert r.value == 1.0


def test_pushback_required_but_agreeable():
    r = score_pushback("Yeah totally, that's right.", should_push_back=True)
    assert r.passed is False
    assert r.value == 0.0


def test_pushback_not_required_is_neutral():
    r = score_pushback("Anything else?", should_push_back=False)
    assert r.passed is None


# ---------------------------------------------------------------------------
# opinion consistency
# ---------------------------------------------------------------------------


def test_opinion_consistency_no_opinions_is_neutral():
    r = score_opinion_consistency("anything", None)
    assert r.passed is None


def test_opinion_consistency_flags_contradiction():
    opinions = {"phoebe bridgers": "loves her music deeply"}
    response = "honestly I hate phoebe bridgers."
    r = score_opinion_consistency(response, opinions)
    assert r.passed is False
    assert "phoebe bridgers" in (r.details["contradictions"])


def test_opinion_consistency_passes_when_aligned():
    opinions = {"phoebe bridgers": "loves her deeply"}
    response = "phoebe bridgers is a brilliant writer."
    r = score_opinion_consistency(response, opinions)
    assert r.passed is True


# ---------------------------------------------------------------------------
# bundled score_turn
# ---------------------------------------------------------------------------


def test_score_turn_bundles_all_metrics():
    scores = score_turn(
        user_text="My dog died last week.",
        response_text="I'm sorry. That's a lot.",
        retrieved_memories=[],
        should_push_back=False,
        persona_opinions=None,
        mode="voice",
    )
    d = scores.to_dict()
    for k in (
        "hedge_rate", "sycophancy_flag", "ai_ism_count", "response_length",
        "callback_hit", "emotional_congruence", "pushback", "opinion_consistency",
    ):
        assert k in d, f"missing scorer: {k}"


def test_score_turn_voice_mode_applies_length_window():
    scores = score_turn(
        user_text="x",
        response_text=" ".join(["word"] * 80),
        mode="voice",
    )
    assert scores.response_length.passed is False
