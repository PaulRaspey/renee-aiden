"""Unit tests for the M9 backchannel layer."""
from __future__ import annotations

import random
import wave
from pathlib import Path

import pytest
import yaml

from src.paralinguistics.injector import ClipLibrary, MoodLike
from src.turn_taking.backchannel import (
    BackchannelContext,
    BackchannelEvent,
    BackchannelLayer,
    BackchannelTrigger,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_silent_wav(path: Path, duration_ms: int = 200, sr: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sr * duration_ms / 1000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n)


@pytest.fixture
def clip_library(tmp_path: Path) -> ClipLibrary:
    root = tmp_path / "paralinguistics" / "renee"
    clips = []
    for cat, sub in [
        ("affirmations", "mhm"),
        ("affirmations", "yeah"),
        ("affirmations", "right"),
        ("thinking", "mm"),
    ]:
        for i in range(1, 4):
            rel = f"{cat}/{sub}/{sub}_{i:03d}.wav"
            _write_silent_wav(root / rel)
            clips.append({
                "file": rel,
                "category": cat,
                "subcategory": sub,
                "emotion": "acknowledging",
                "intensity": 0.35,
                "energy_level": 0.4,
                "tags": [],
                "appropriate_contexts": [],
                "inappropriate_contexts": [],
                "duration_ms": 200,
                "sample_rate": 24000,
            })
    (root / "metadata.yaml").write_text(
        yaml.safe_dump({"voice": "renee", "clips": clips}),
        encoding="utf-8",
    )
    return ClipLibrary(root)


@pytest.fixture
def layer(clip_library) -> BackchannelLayer:
    return BackchannelLayer(
        clip_library,
        min_gap_ms=1000,
        max_per_minute=20,
        base_probability=0.9,
        rng=random.Random(7),
    )


# ---------------------------------------------------------------------------
# triggers
# ---------------------------------------------------------------------------


def test_clause_boundary_fires_on_comma_and_silence(layer):
    trig = layer.detect_opportunity("So I was saying,", silence_ms=250)
    assert trig == BackchannelTrigger.CLAUSE_BOUNDARY


def test_connective_ending_is_clause_boundary(layer):
    trig = layer.detect_opportunity("We went to the store and", silence_ms=220)
    assert trig == BackchannelTrigger.CLAUSE_BOUNDARY


def test_question_is_confirmation_seeking(layer):
    trig = layer.detect_opportunity("You know what I mean?", silence_ms=100)
    assert trig == BackchannelTrigger.CONFIRMATION_SEEKING


def test_rising_intonation_flag_detected(layer):
    trig = layer.detect_opportunity("We went", silence_ms=150, rising_intonation=True)
    assert trig == BackchannelTrigger.RISING_INTONATION


def test_emotional_content_detected(layer):
    trig = layer.detect_opportunity("My dad died last year", silence_ms=120)
    assert trig == BackchannelTrigger.EMOTIONAL_CONTENT


def test_intimate_moment_on_quiet_low_energy(layer):
    trig = layer.detect_opportunity(
        "I've been up since five",
        silence_ms=400,
        energy_low=True,
    )
    assert trig == BackchannelTrigger.INTIMATE_MOMENT


def test_empty_transcript_returns_none(layer):
    assert layer.detect_opportunity("", silence_ms=300) is None


def test_no_trigger_mid_word(layer):
    assert layer.detect_opportunity("I wen", silence_ms=50) is None


# ---------------------------------------------------------------------------
# hard rules
# ---------------------------------------------------------------------------


def test_disagreement_blocks_firing(layer):
    ctx = BackchannelContext(user_speaking=True, is_disagreement=True, mood=MoodLike(warmth=0.9))
    p = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, ctx, now_ms=2000.0)
    assert p == 0.0


def test_user_distress_blocks_firing(layer):
    ctx = BackchannelContext(user_speaking=True, user_distressed=True, mood=MoodLike(warmth=0.9))
    p = layer.should_fire(BackchannelTrigger.EMOTIONAL_CONTENT, ctx, now_ms=2000.0)
    assert p == 0.0


def test_heated_tone_blocks_firing(layer):
    ctx = BackchannelContext(user_speaking=True, conversation_tone="heated", mood=MoodLike(warmth=0.9))
    p = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, ctx, now_ms=2000.0)
    assert p == 0.0


def test_renee_speaking_blocks_firing(layer):
    ctx = BackchannelContext(user_speaking=False, mood=MoodLike(warmth=0.9))
    p = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, ctx, now_ms=2000.0)
    assert p == 0.0


# ---------------------------------------------------------------------------
# rate caps
# ---------------------------------------------------------------------------


def test_min_gap_between_fires(layer):
    ctx = BackchannelContext(mood=MoodLike(warmth=0.9))
    ev1 = layer.observe("Yeah,", silence_ms=250, context=ctx, now_ms=0.0)
    assert ev1 is not None
    # Immediately afterwards, no fire.
    ev2 = layer.observe("So anyway,", silence_ms=250, context=ctx, now_ms=200.0)
    assert ev2 is None
    # After the gap has elapsed, the gate allows firing again.
    p = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, ctx, now_ms=1200.0)
    assert p > 0.0


def test_max_per_minute_enforced(clip_library):
    # Base prob high enough to reliably fire when allowed.
    layer = BackchannelLayer(
        clip_library,
        min_gap_ms=10,
        max_per_minute=3,
        base_probability=1.0,
        rng=random.Random(1),
    )
    ctx = BackchannelContext(mood=MoodLike(warmth=0.9))
    fires = 0
    for t in range(20):
        ev = layer.observe(
            "yeah,",
            silence_ms=250,
            context=ctx,
            now_ms=t * 100.0,
        )
        if ev is not None:
            fires += 1
    assert fires <= 3


# ---------------------------------------------------------------------------
# probability scaling
# ---------------------------------------------------------------------------


def test_warmth_scales_fire_probability_up(layer):
    cold = BackchannelContext(mood=MoodLike(warmth=0.2))
    warm = BackchannelContext(mood=MoodLike(warmth=0.95))
    p_cold = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, cold, now_ms=2000.0)
    p_warm = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, warm, now_ms=2000.0)
    assert p_warm > p_cold


def test_playful_tone_increases_probability(layer):
    casual = BackchannelContext(conversation_tone="casual", mood=MoodLike(warmth=0.7))
    playful = BackchannelContext(conversation_tone="playful", mood=MoodLike(warmth=0.7))
    p_cas = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, casual, now_ms=2000.0)
    p_pl = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, playful, now_ms=2000.0)
    assert p_pl > p_cas


def test_serious_tone_decreases_probability(layer):
    casual = BackchannelContext(conversation_tone="casual", mood=MoodLike(warmth=0.7))
    serious = BackchannelContext(conversation_tone="serious", mood=MoodLike(warmth=0.7))
    p_cas = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, casual, now_ms=2000.0)
    p_ser = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, serious, now_ms=2000.0)
    assert p_ser < p_cas


def test_intimacy_scales_probability(layer):
    low = BackchannelContext(intimacy=0.1, mood=MoodLike(warmth=0.7))
    high = BackchannelContext(intimacy=0.9, mood=MoodLike(warmth=0.7))
    p_low = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, low, now_ms=2000.0)
    p_hi = layer.should_fire(BackchannelTrigger.CLAUSE_BOUNDARY, high, now_ms=2000.0)
    assert p_hi > p_low


# ---------------------------------------------------------------------------
# token selection + clip binding
# ---------------------------------------------------------------------------


def test_token_bound_to_library_clip(layer):
    ctx = BackchannelContext(mood=MoodLike(warmth=0.9))
    ev = layer.observe("Yeah,", silence_ms=250, context=ctx, now_ms=0.0)
    assert ev is not None
    assert ev.token.clip_path is not None
    assert ev.token.clip_path.exists()


def test_token_volume_default_minus_six_db(layer):
    ctx = BackchannelContext(mood=MoodLike(warmth=0.9))
    ev = layer.observe("Yeah,", silence_ms=250, context=ctx, now_ms=0.0)
    assert ev is not None
    assert ev.token.volume_db == -6.0


def test_emotional_content_prefers_soft_tokens(clip_library):
    layer = BackchannelLayer(
        clip_library,
        min_gap_ms=10,
        base_probability=1.0,
        rng=random.Random(42),
    )
    ctx = BackchannelContext(mood=MoodLike(warmth=0.9))
    # Sample many picks and check emotional content rarely picks 'right'.
    seen_subs: list[str] = []
    for i in range(12):
        token = layer.pick_token(BackchannelTrigger.EMOTIONAL_CONTENT, ctx)
        assert token is not None
        seen_subs.append(token.subcategory)
    # should mostly be 'mhm' or 'mm', never 'right' or 'yeah'
    assert "right" not in seen_subs
    assert "yeah" not in seen_subs


def test_warmth_biases_toward_affirmative_tokens(clip_library):
    layer = BackchannelLayer(
        clip_library,
        min_gap_ms=10,
        base_probability=1.0,
        rng=random.Random(123),
    )
    ctx = BackchannelContext(mood=MoodLike(warmth=0.95))
    counts: dict[str, int] = {}
    for _ in range(60):
        token = layer.pick_token(BackchannelTrigger.CLAUSE_BOUNDARY, ctx)
        counts[token.subcategory] = counts.get(token.subcategory, 0) + 1
    # With high warmth, "mhm" should dominate or at least out-draw "mm".
    assert counts.get("mhm", 0) >= counts.get("mm", 0)


# ---------------------------------------------------------------------------
# deterministic RNG
# ---------------------------------------------------------------------------


def test_same_seed_same_trace(clip_library):
    def run(seed):
        layer = BackchannelLayer(
            clip_library,
            min_gap_ms=100,
            base_probability=0.8,
            rng=random.Random(seed),
        )
        ctx = BackchannelContext(mood=MoodLike(warmth=0.8))
        events = []
        for t in range(12):
            ev = layer.observe(
                "yeah,",
                silence_ms=250,
                context=ctx,
                now_ms=float(t * 300),
            )
            events.append(None if ev is None else (ev.token.category, ev.token.subcategory, ev.reason))
        return events

    assert run(99) == run(99)


def test_observe_returns_none_outside_opportunities(layer):
    ctx = BackchannelContext(mood=MoodLike(warmth=0.9))
    ev = layer.observe("I was saying", silence_ms=60, context=ctx, now_ms=0.0)
    assert ev is None


def test_reset_clears_gap_and_history(clip_library):
    layer = BackchannelLayer(
        clip_library,
        min_gap_ms=5_000,
        base_probability=1.0,
        rng=random.Random(0),
    )
    ctx = BackchannelContext(mood=MoodLike(warmth=0.9))
    ev = layer.observe("yeah,", silence_ms=250, context=ctx, now_ms=0.0)
    assert ev is not None
    # gap would block
    assert layer.observe("yeah,", silence_ms=250, context=ctx, now_ms=500.0) is None
    layer.reset()
    ev2 = layer.observe("yeah,", silence_ms=250, context=ctx, now_ms=600.0)
    assert ev2 is not None
