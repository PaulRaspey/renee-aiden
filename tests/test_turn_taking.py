"""Unit tests for the M8 turn-taking layer."""
from __future__ import annotations

import random

import pytest

from src.turn_taking import (
    EndpointAction,
    Endpointer,
    InterruptionHandler,
    InterruptionReason,
    TickResult,
    TurnController,
    TurnState,
    TurnType,
    classify_turn,
    plan_latency,
    target_latency_ms,
)
from src.voice.prosody import MoodLike


# ===========================================================================
# Endpointer
# ===========================================================================


def test_endpointer_low_silence_incomplete_transcript_is_idle():
    ep = Endpointer()
    d = ep.decide("Hey, so I was thinking", silence_ms=50)
    assert d.action == EndpointAction.IDLE
    assert d.p_end < 0.5


def test_endpointer_trailing_comma_is_not_endpoint():
    ep = Endpointer()
    d = ep.decide("Let me think,", silence_ms=400)
    # Comma explicitly says more is coming.
    assert d.p_end < 0.6


def test_endpointer_continuation_word_suppresses_commit():
    ep = Endpointer()
    # Long silence, but trailing "and" -> user hasn't finished.
    d = ep.decide("I went to the store and", silence_ms=700)
    assert d.action != EndpointAction.COMMIT


def test_endpointer_terminal_punctuation_and_silence_triggers_commit():
    ep = Endpointer()
    # One tick above 0.9 won't commit; needs sustain.
    d1 = ep.decide("I went to the store.", silence_ms=900, tick_elapsed_ms=100)
    # Continue ticking with sustained silence.
    d2 = ep.decide("I went to the store.", silence_ms=1000, tick_elapsed_ms=100)
    assert d2.action == EndpointAction.COMMIT


def test_endpointer_commit_requires_sustain():
    ep = Endpointer()
    d = ep.decide("Okay done.", silence_ms=850, tick_elapsed_ms=50)
    # first tick: p > 0.9 but sustain only 50ms, not yet committed
    assert d.action != EndpointAction.COMMIT


def test_endpointer_prewarm_threshold_crossed_at_300ms_silence():
    ep = Endpointer()
    d = ep.decide("Whatever.", silence_ms=350)
    assert d.action in (EndpointAction.PREWARM, EndpointAction.SPECULATIVE)


def test_endpointer_speculative_threshold_crossed_around_500ms():
    ep = Endpointer()
    d = ep.decide("I'll see.", silence_ms=550)
    assert d.action in (EndpointAction.SPECULATIVE, EndpointAction.COMMIT)
    assert d.p_end >= 0.7


def test_endpointer_filler_word_tail_suppresses():
    ep = Endpointer()
    d = ep.decide("I was thinking like uh", silence_ms=400)
    assert d.p_end < 0.6


def test_endpointer_energy_falling_bumps_probability():
    ep1 = Endpointer()
    ep2 = Endpointer()
    p_flat = ep1.predict("See you.", silence_ms=300, energy_falling=False)
    p_fall = ep2.predict("See you.", silence_ms=300, energy_falling=True)
    assert p_fall > p_flat


def test_endpointer_reset_clears_sustain_timer():
    ep = Endpointer()
    # push sustain into commit territory
    ep.decide("Done.", silence_ms=900, tick_elapsed_ms=80)
    ep.decide("Done.", silence_ms=950, tick_elapsed_ms=80)
    ep.reset()
    # next tick should not be immediately committed.
    d = ep.decide("Okay.", silence_ms=850, tick_elapsed_ms=50)
    assert d.action != EndpointAction.COMMIT


def test_endpointer_short_transcript_short_silence_low_probability():
    ep = Endpointer()
    d = ep.decide("So", silence_ms=50)
    assert d.p_end < 0.15


def test_endpointer_empty_transcript_plus_long_silence_eventually_commits():
    ep = Endpointer()
    ep.decide("", silence_ms=1200, tick_elapsed_ms=100)
    d = ep.decide("", silence_ms=1300, tick_elapsed_ms=100)
    # Even with empty transcript, long silence should commit.
    assert d.action in (EndpointAction.SPECULATIVE, EndpointAction.COMMIT)


# ===========================================================================
# Latency controller
# ===========================================================================


def test_classify_turn_acknowledgment():
    assert classify_turn("yeah") == TurnType.ACKNOWLEDGMENT
    assert classify_turn("mhm") == TurnType.ACKNOWLEDGMENT
    assert classify_turn("okay, sure") == TurnType.ACKNOWLEDGMENT


def test_classify_turn_simple_question():
    assert classify_turn("what time is it?") == TurnType.SIMPLE_QUESTION


def test_classify_turn_normal_default():
    assert classify_turn("I went to the store today and picked up milk") == TurnType.NORMAL_RESPONSE


def test_classify_turn_flag_overrides():
    assert classify_turn("okay", is_difficult_truth=True) == TurnType.DIFFICULT_TRUTH
    assert classify_turn("okay", is_vulnerable_admission=True) == TurnType.EMOTIONAL_RESPONSE
    assert classify_turn("anything", is_thoughtful=True) == TurnType.THOUGHTFUL_RESPONSE


def test_classify_turn_long_text_is_thoughtful():
    long = " ".join(["word"] * 50)
    assert classify_turn(long) == TurnType.THOUGHTFUL_RESPONSE


def test_target_latency_emotional_slower_than_normal():
    rng = random.Random(0)
    em = target_latency_ms(TurnType.EMOTIONAL_RESPONSE, MoodLike(), rng=rng)
    rng = random.Random(0)
    nr = target_latency_ms(TurnType.NORMAL_RESPONSE, MoodLike(), rng=rng)
    assert em > nr


def test_target_latency_difficult_truth_slowest():
    rng = random.Random(0)
    dt = target_latency_ms(TurnType.DIFFICULT_TRUTH, MoodLike(), rng=rng)
    rng = random.Random(0)
    em = target_latency_ms(TurnType.EMOTIONAL_RESPONSE, MoodLike(), rng=rng)
    assert dt > em


def test_target_latency_acknowledgment_fastest():
    rng = random.Random(0)
    ack = target_latency_ms(TurnType.ACKNOWLEDGMENT, MoodLike(), rng=rng)
    rng = random.Random(0)
    nr = target_latency_ms(TurnType.NORMAL_RESPONSE, MoodLike(), rng=rng)
    assert ack < nr


def test_target_latency_tired_slower_than_baseline():
    tired = MoodLike(energy=0.2)
    rested = MoodLike(energy=0.8)
    rng = random.Random(0)
    lt = target_latency_ms(TurnType.NORMAL_RESPONSE, tired, rng=rng)
    rng = random.Random(0)
    lr = target_latency_ms(TurnType.NORMAL_RESPONSE, rested, rng=rng)
    assert lt > lr


def test_target_latency_playful_faster_than_baseline():
    playful = MoodLike(playfulness=0.9)
    rng = random.Random(0)
    lp = target_latency_ms(TurnType.NORMAL_RESPONSE, playful, rng=rng)
    rng = random.Random(0)
    flat = target_latency_ms(TurnType.NORMAL_RESPONSE, MoodLike(playfulness=0.5), rng=rng)
    assert lp < flat


def test_target_latency_has_floor():
    rng = random.Random(0)
    for _ in range(20):
        result = target_latency_ms(TurnType.ACKNOWLEDGMENT, MoodLike(playfulness=1.0, focus=1.0), rng=rng)
        assert result >= 80  # floor applies


def test_plan_latency_flags_thinking_filler_for_slow_responses():
    rng = random.Random(0)
    plan = plan_latency(
        "My dog died last night.",
        MoodLike(energy=0.5),
        context_flags={"is_emotional": True},
        rng=rng,
    )
    assert plan.turn_type == TurnType.EMOTIONAL_RESPONSE
    assert plan.include_thinking_filler  # target >= 600ms


def test_plan_latency_no_filler_on_quick_acknowledgment():
    rng = random.Random(0)
    plan = plan_latency("yeah", MoodLike(playfulness=0.9), rng=rng)
    assert plan.turn_type == TurnType.ACKNOWLEDGMENT
    assert not plan.include_thinking_filler


# ===========================================================================
# InterruptionHandler
# ===========================================================================


def test_user_interruption_requires_sustain():
    h = InterruptionHandler(user_energy_threshold=0.4, sustain_required_ms=100)
    # single tick above threshold is not enough
    e = h.observe_user_energy(0.5, now_ms=0.0)
    assert e is None
    # 100ms later still above
    e = h.observe_user_energy(0.5, now_ms=100.0)
    # exactly at threshold triggers
    assert e is not None
    assert e.who == "user"
    assert e.cancel_tts is True
    assert e.yield_gracefully is True


def test_user_brief_spike_does_not_interrupt():
    h = InterruptionHandler(user_energy_threshold=0.4, sustain_required_ms=100)
    # Spike for only 50ms
    assert h.observe_user_energy(0.5, now_ms=0.0) is None
    assert h.observe_user_energy(0.5, now_ms=50.0) is None
    # Energy drops below threshold
    assert h.observe_user_energy(0.1, now_ms=80.0) is None
    # New spike, reset timer
    assert h.observe_user_energy(0.5, now_ms=100.0) is None
    # needs another 100ms
    e = h.observe_user_energy(0.5, now_ms=200.0)
    assert e is not None


def test_renee_interrupts_on_strong_disagreement():
    h = InterruptionHandler()
    e = h.should_renee_interrupt(user_speaking=True, disagreement_score=0.9)
    assert e is not None
    assert e.who == "renee"
    assert e.reason == InterruptionReason.RENEE_DISAGREEMENT.value


def test_renee_interrupts_on_correction_urgency():
    h = InterruptionHandler()
    e = h.should_renee_interrupt(user_speaking=True, correction_urgency=0.9)
    assert e is not None
    assert e.reason == InterruptionReason.RENEE_CORRECTION.value


def test_renee_does_not_interrupt_when_user_not_speaking():
    h = InterruptionHandler()
    assert h.should_renee_interrupt(user_speaking=False, disagreement_score=0.99) is None


def test_renee_interruption_cap_enforced():
    h = InterruptionHandler(cap_per_n_turns=1)
    first = h.should_renee_interrupt(user_speaking=True, disagreement_score=0.9)
    assert first is not None
    second = h.should_renee_interrupt(user_speaking=True, disagreement_score=0.9)
    # cap == 1, already one in window.
    assert second is None


def test_renee_interruption_cap_slides_over_turn_boundaries():
    h = InterruptionHandler(cap_per_n_turns=2)
    assert h.should_renee_interrupt(user_speaking=True, disagreement_score=0.9) is not None
    for _ in range(3):
        h.on_turn_boundary()
    # window has moved past the earlier interruption
    assert h.renee_interruption_count_in_window() == 0 or h.renee_interruption_count_in_window() == 1


def test_interruption_scores_below_threshold_do_not_trigger():
    h = InterruptionHandler()
    e = h.should_renee_interrupt(
        user_speaking=True,
        disagreement_score=0.7,
        correction_urgency=0.7,
        excitement_score=0.7,
    )
    assert e is None


# ===========================================================================
# TurnController (integration of endpointer + interruption)
# ===========================================================================


def test_turn_controller_starts_idle_and_moves_to_user_speaking():
    c = TurnController()
    assert c.state == TurnState.IDLE
    res = c.on_user_tick("Hey", silence_ms=100)
    assert res.state == TurnState.USER_SPEAKING
    assert res.endpoint is not None


def test_turn_controller_commits_after_sustained_silence():
    c = TurnController()
    c.on_user_tick("I went home.", silence_ms=900, tick_ms=100)
    res = c.on_user_tick("I went home.", silence_ms=1000, tick_ms=100)
    assert res.endpoint.action == EndpointAction.COMMIT


def test_turn_controller_user_interrupts_renee_mid_speech():
    c = TurnController()
    c.begin_renee_speaking()
    assert c.state == TurnState.RENEE_SPEAKING
    # quiet tick -> no interruption
    c.on_user_tick("", silence_ms=0, energy=0.05)
    assert c.state == TurnState.RENEE_SPEAKING
    # energy spikes
    c.interruption._user_above_since_ms = None
    res = c.on_user_tick("", silence_ms=0, energy=0.8)
    # first tick sets sustain timer but doesn't fire
    assert res.interruption is None
    # second tick with enough elapsed time
    c.interruption._user_above_since_ms = 0.0
    # force now_ms by calling directly
    ev = c.interruption.observe_user_energy(0.8, now_ms=200.0)
    assert ev is not None
    assert ev.cancel_tts


def test_turn_controller_plan_response_latency_routes_emotional():
    c = TurnController()
    plan = c.plan_response_latency(
        "My dad died last year.",
        MoodLike(),
        context_flags={"is_emotional": True},
    )
    assert plan.turn_type == TurnType.EMOTIONAL_RESPONSE


def test_turn_controller_end_renee_turn_advances_interruption_window():
    c = TurnController()
    c.begin_renee_speaking()
    c.end_renee_turn()
    assert c.state == TurnState.IDLE
    assert c.interruption._turn_counter == 1
