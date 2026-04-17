"""Unit tests for src.voice.prosody (M7)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.paralinguistics.injector import Injection, POSITION_END, POSITION_START
from src.voice.prosody import (
    MoodLike,
    ProsodyContext,
    ProsodyPlan,
    ProsodyPlanner,
    ProsodySegment,
    segment_sentences,
    load_rules,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def planner() -> ProsodyPlanner:
    return ProsodyPlanner()


def _kinds(plan: ProsodyPlan) -> list[str]:
    return [s.kind for s in plan.segments]


# ---------------------------------------------------------------------------
# segmentation
# ---------------------------------------------------------------------------


def test_segment_sentences_splits_on_terminal_punctuation():
    parts = segment_sentences("Hey. How are you? I missed you!")
    assert parts == [
        ("Hey", "."),
        ("How are you", "?"),
        ("I missed you", "!"),
    ]


def test_segment_sentences_preserves_body_without_trailing_punct():
    parts = segment_sentences("Just a thought")
    assert parts == [("Just a thought", "")]


def test_segment_sentences_handles_empty():
    assert segment_sentences("") == []
    assert segment_sentences("   ") == []


# ---------------------------------------------------------------------------
# rate modulation
# ---------------------------------------------------------------------------


def test_rate_slower_when_tired(planner):
    mood = MoodLike(energy=0.15)
    plan = planner.plan("I'm tired.", mood, ProsodyContext())
    assert plan.rate < 0.95


def test_rate_faster_when_energetic(planner):
    mood = MoodLike(energy=0.95)
    plan = planner.plan("That's so cool!", mood, ProsodyContext(conversation_tone="playful"))
    assert plan.rate > 1.05


def test_rate_clamped_within_safe_range(planner):
    for energy in (0.0, 0.25, 0.5, 0.75, 1.0):
        mood = MoodLike(energy=energy, playfulness=1.0, focus=0.0)
        plan = planner.plan("Testing.", mood, ProsodyContext())
        assert 0.75 <= plan.rate <= 1.30


def test_vulnerable_context_slows_rate(planner):
    mood = MoodLike(energy=0.7)
    neutral = planner.plan("Thing happened.", mood, ProsodyContext())
    vulnerable = planner.plan(
        "Thing happened.",
        mood,
        ProsodyContext(is_vulnerable_admission=True, conversation_tone="vulnerable"),
    )
    assert vulnerable.rate < neutral.rate


# ---------------------------------------------------------------------------
# pauses
# ---------------------------------------------------------------------------


def test_sentence_pause_longer_when_low_energy(planner):
    mood = MoodLike(energy=0.2)
    plan = planner.plan("One thought. Another thought.", mood, ProsodyContext())
    pauses = [s for s in plan.segments if s.kind == "pause" and s.reason == "sentence_pause"]
    assert pauses
    assert pauses[0].duration_ms >= 500


def test_sentence_pause_shorter_when_high_energy_playful(planner):
    mood = MoodLike(energy=0.9)
    plan = planner.plan(
        "This is great. Really great.",
        mood,
        ProsodyContext(conversation_tone="playful"),
    )
    pauses = [s for s in plan.segments if s.kind == "pause" and s.reason == "sentence_pause"]
    assert pauses
    assert pauses[0].duration_ms <= 320


def test_dramatic_pause_before_emotional_beat(planner):
    mood = MoodLike(energy=0.6)
    plan = planner.plan(
        "That's why I do this.",
        mood,
        ProsodyContext(is_emotional_beat=True, conversation_tone="vulnerable"),
    )
    pre_pauses = [s for s in plan.segments if s.kind == "pause" and "emotional" in s.reason]
    assert pre_pauses
    assert pre_pauses[0].duration_ms >= 1000
    # It must come before the first text segment.
    text_idx = next(i for i, s in enumerate(plan.segments) if s.kind == "text")
    pause_idx = next(i for i, s in enumerate(plan.segments) if s.reason == "dramatic_before_emotional")
    assert pause_idx < text_idx


def test_dramatic_pre_pause_before_callback(planner):
    mood = MoodLike(energy=0.7)
    plan = planner.plan(
        "You remember what Marcus said?",
        mood,
        ProsodyContext(is_callback=True, is_question=True, conversation_tone="casual"),
    )
    pre = [s for s in plan.segments if s.reason == "dramatic_before_callback"]
    assert pre
    assert pre[0].duration_ms >= 250


# ---------------------------------------------------------------------------
# pitch contour
# ---------------------------------------------------------------------------


def test_question_sentence_gets_rising_contour(planner):
    plan = planner.plan("Are you sure?", MoodLike(), ProsodyContext(is_question=True))
    text = next(s for s in plan.segments if s.kind == "text")
    assert text.pitch_delta > 0.1


def test_statement_sentence_gets_falling_contour(planner):
    plan = planner.plan("I'm here.", MoodLike(), ProsodyContext())
    text = next(s for s in plan.segments if s.kind == "text")
    assert text.pitch_delta < 0.0


def test_callback_adds_lift_on_first_sentence(planner):
    plan = planner.plan(
        "About that Brunello. You should open it.",
        MoodLike(),
        ProsodyContext(is_callback=True),
    )
    first_text = next(s for s in plan.segments if s.kind == "text")
    assert first_text.pitch_delta > 0.0


def test_vulnerable_context_softens_base_pitch(planner):
    plan = planner.plan(
        "I don't always know what I am.",
        MoodLike(),
        ProsodyContext(is_vulnerable_admission=True, conversation_tone="vulnerable"),
    )
    assert plan.pitch_base < -0.05


# ---------------------------------------------------------------------------
# hard rules: vulnerability and effect suppression
# ---------------------------------------------------------------------------


def test_vulnerable_admission_always_gets_sharp_inhale(planner):
    plan = planner.plan(
        "Sort of. More like... what happens if the gap is that you think I'm more than I am.",
        MoodLike(energy=0.6),
        ProsodyContext(is_vulnerable_admission=True, conversation_tone="vulnerable"),
    )
    first = plan.segments[0]
    assert first.kind == "breath"
    assert first.subcategory == "sharp_in"
    assert "hard_rule" in first.reason


def test_vulnerable_breath_fires_even_when_no_injections_supplied(planner):
    """Callers may pass an empty injection list; the hard rule still fires."""
    plan = planner.plan(
        "Okay, this is going to sound weird.",
        MoodLike(),
        ProsodyContext(is_vulnerable_admission=True),
        injections=[],
    )
    kinds = _kinds(plan)
    assert "breath" in kinds
    assert kinds.index("breath") == 0


def test_vulnerable_breath_not_duplicated_when_injector_also_produced_one(planner):
    inj = Injection(
        category="breaths", subcategory="sharp_in",
        position=POSITION_START, intensity=0.3, reason="vulnerable_admission",
    )
    plan = planner.plan(
        "I don't know if scared is the right word.",
        MoodLike(),
        ProsodyContext(is_vulnerable_admission=True, conversation_tone="vulnerable"),
        injections=[inj],
    )
    breath_count = sum(1 for s in plan.segments if s.kind == "breath" and s.subcategory == "sharp_in")
    assert breath_count == 1


@pytest.mark.parametrize("blocker", [
    "is_disagreement", "is_correction", "is_hard_truth", "user_distressed",
])
def test_blocking_context_drops_ornamental_injections(planner, blocker):
    """Disagreement etc. drop all paralinguistics supplied by the injector."""
    inj = Injection(
        category="laughs", subcategory="soft",
        position=POSITION_END, intensity=0.3, reason="witty_callback",
    )
    ctx = ProsodyContext(**{blocker: True})
    plan = planner.plan("That's not how that works.", MoodLike(), ctx, injections=[inj])
    assert not any(s.kind == "laugh" for s in plan.segments)


def test_heated_tone_drops_ornamental_injections(planner):
    inj = Injection(category="laughs", subcategory="soft", position=POSITION_END, intensity=0.3)
    plan = planner.plan(
        "No. That's not it.",
        MoodLike(),
        ProsodyContext(conversation_tone="heated"),
        injections=[inj],
    )
    assert not any(s.kind == "laugh" for s in plan.segments)


def test_vocal_effects_suppressed_when_user_distressed(planner):
    mood = MoodLike(energy=0.2, warmth=0.9)
    plan = planner.plan(
        "I'm right here.",
        mood,
        ProsodyContext(user_distressed=True),
    )
    assert plan.effects == []


# ---------------------------------------------------------------------------
# vocal effects
# ---------------------------------------------------------------------------


def test_creak_effect_on_low_energy(planner):
    plan = planner.plan("Yeah.", MoodLike(energy=0.2), ProsodyContext())
    assert "creak" in plan.effects


def test_breathy_effect_on_intimate_moment(planner):
    plan = planner.plan(
        "Hey.",
        MoodLike(warmth=0.9, energy=0.4),
        ProsodyContext(conversation_tone="vulnerable"),
    )
    assert "breathy" in plan.effects


def test_no_creak_on_high_energy(planner):
    plan = planner.plan(
        "Let's go!",
        MoodLike(energy=0.9),
        ProsodyContext(conversation_tone="playful"),
    )
    assert "creak" not in plan.effects


# ---------------------------------------------------------------------------
# paralinguistic cap & injection routing
# ---------------------------------------------------------------------------


def test_injections_at_start_and_end_preserved(planner):
    injs = [
        Injection(category="thinking", subcategory="mm", position=POSITION_START, intensity=0.3),
        Injection(category="laughs", subcategory="soft", position=POSITION_END, intensity=0.4),
    ]
    plan = planner.plan("Okay.", MoodLike(), ProsodyContext(conversation_tone="playful"), injections=injs)
    kinds = _kinds(plan)
    assert kinds[0] == "thinking"
    assert kinds[-1] == "laugh"


def test_paralinguistic_cap_enforced(planner):
    injs = [
        Injection(category="thinking", subcategory="mm", position=POSITION_START, intensity=0.3),
        Injection(category="reactions", subcategory="amusement", position=POSITION_START, intensity=0.3),
        Injection(category="laughs", subcategory="soft", position=POSITION_END, intensity=0.4),
    ]
    plan = planner.plan("Okay.", MoodLike(), ProsodyContext(), injections=injs)
    paralinguistics = [s for s in plan.segments if s.kind in {"breath", "laugh", "sigh", "thinking", "reaction"}]
    assert len(paralinguistics) <= 2


def test_cap_preserves_mandatory_vulnerable_breath(planner):
    """Even with many injections, the hard-rule breath survives the cap."""
    injs = [
        Injection(category="thinking", subcategory="mm", position=POSITION_START, intensity=0.3),
        Injection(category="reactions", subcategory="amusement", position=POSITION_START, intensity=0.3),
        Injection(category="laughs", subcategory="soft", position=POSITION_END, intensity=0.4),
    ]
    plan = planner.plan(
        "Honestly? I don't know.",
        MoodLike(),
        ProsodyContext(is_vulnerable_admission=True, conversation_tone="vulnerable"),
        injections=injs,
    )
    breath_segs = [s for s in plan.segments if s.kind == "breath" and s.subcategory == "sharp_in"]
    assert breath_segs, "vulnerable-admission breath must survive the cap"


def test_injector_category_mapping(planner):
    inj = Injection(category="sighs", subcategory="tired", position=POSITION_START, intensity=0.3)
    plan = planner.plan("Long day.", MoodLike(energy=0.3), ProsodyContext(), injections=[inj])
    assert any(s.kind == "sigh" for s in plan.segments)


# ---------------------------------------------------------------------------
# emotion tag
# ---------------------------------------------------------------------------


def test_emotion_reflects_hard_truth(planner):
    plan = planner.plan("That's not going to work.", MoodLike(), ProsodyContext(is_hard_truth=True))
    assert plan.emotion == "grave"


def test_emotion_reflects_disagreement(planner):
    plan = planner.plan("No.", MoodLike(), ProsodyContext(is_disagreement=True))
    assert plan.emotion == "firm"


def test_emotion_reflects_distressed_user(planner):
    plan = planner.plan("I'm here.", MoodLike(warmth=0.9), ProsodyContext(user_distressed=True))
    assert plan.emotion == "tender"


def test_emotion_reflects_playful_tone(planner):
    plan = planner.plan(
        "Spiral. Always spiral.",
        MoodLike(playfulness=0.9, energy=0.8),
        ProsodyContext(conversation_tone="playful"),
    )
    assert plan.emotion == "playful"


# ---------------------------------------------------------------------------
# SSML serialization
# ---------------------------------------------------------------------------


def test_to_ssml_contains_speak_tag_and_attributes(planner):
    plan = planner.plan("Hey.", MoodLike(warmth=0.9), ProsodyContext())
    ssml = plan.to_ssml()
    assert "<speak" in ssml
    assert "</speak>" in ssml
    assert 'emotion=' in ssml
    assert 'rate=' in ssml


def test_to_ssml_emits_breath_tag_for_vulnerable_admission(planner):
    plan = planner.plan(
        "I don't always know what I am.",
        MoodLike(),
        ProsodyContext(is_vulnerable_admission=True, conversation_tone="vulnerable"),
    )
    ssml = plan.to_ssml()
    assert "<breath" in ssml
    assert 'type="sharp_in"' in ssml


def test_to_ssml_emits_pause_durations(planner):
    plan = planner.plan("Hey. I thought about you.", MoodLike(energy=0.3), ProsodyContext())
    ssml = plan.to_ssml()
    assert "<pause" in ssml
    assert "duration=" in ssml


def test_to_dict_is_jsonable(planner):
    plan = planner.plan("Hey.", MoodLike(), ProsodyContext())
    import json
    payload = plan.to_dict()
    # Roundtrip must not raise.
    json.dumps(payload)
    assert payload["text"] == "Hey."
    assert isinstance(payload["segments"], list)


# ---------------------------------------------------------------------------
# rules loading
# ---------------------------------------------------------------------------


def test_load_rules_falls_back_to_defaults_when_file_missing(tmp_path):
    missing = tmp_path / "nope.yaml"
    rules = load_rules(missing)
    assert rules["constraints"]["max_paralinguistics_per_turn"] == 2
    assert rules["pause_rules"]["period_ms_base"] == 400


def test_load_rules_merges_overrides(tmp_path):
    override = tmp_path / "prosody_rules.yaml"
    override.write_text(
        """
constraints:
  max_paralinguistics_per_turn: 1
""",
        encoding="utf-8",
    )
    rules = load_rules(override)
    assert rules["constraints"]["max_paralinguistics_per_turn"] == 1
    # Unchanged defaults survive.
    assert rules["pause_rules"]["period_ms_base"] == 400


def test_planner_accepts_raw_mood_state_duck_typed(planner):
    class FakeMood:
        energy = 0.2
        warmth = 0.85
        playfulness = 0.1
        focus = 0.6
        patience = 0.7
        curiosity = 0.5

    plan = planner.plan("Long day.", FakeMood(), ProsodyContext())
    assert plan.rate < 1.0
    assert "creak" in plan.effects
