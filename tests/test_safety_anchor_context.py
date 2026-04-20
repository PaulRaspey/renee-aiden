"""Tests for context-aware reality-anchor suppression (Phase 2a, pre-M15).

The reality-anchor injector must not fire on turns that carry load-bearing
emotional weight. PersonaCore computes ctx_flags from the inferred user tone
and vulnerability markers in the user text; the injector reads those flags
and stands down.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from src.persona.core import (
    PersonaCore,
    _build_anchor_ctx_flags,
    _contains_vulnerability_marker,
    _infer_user_tone,
)
from src.persona.llm_router import LLMResponse
from src.persona.mood import MoodState
from src.safety import SafetyLayer
from src.safety.config import (
    HealthMonitorConfig,
    PIIScrubberConfig,
    RealityAnchorsConfig,
    SafetyConfig,
)


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeRouter:
    """Returns a canned neutral reply. Never touches the network."""

    def __init__(self, response_text: str = "Yeah, I'm here."):
        self.response_text = response_text

    def decide_backend(self, user_text: str, expected_depth: str = "normal") -> str:
        return "fake"

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        backend: str | None = None,
        temperature: float = 0.85,
        max_tokens: int = 400,
        user_text: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            text=self.response_text,
            backend="fake",
            model="fake-1",
            latency_ms=1.0,
            input_tokens=5,
            output_tokens=5,
        )


def _forced_safety_layer(state_dir: Path, *, rate_denominator: int = 1) -> SafetyLayer:
    """Build a SafetyLayer whose anchor injector fires on every eligible roll.

    `rate_denominator=1` makes the underlying randint a guaranteed hit, so if
    the injector still declines to fire we know the suppression path did the
    work (rather than a lucky roll).
    """
    cfg = SafetyConfig(
        reality_anchors=RealityAnchorsConfig(
            enabled=True,
            rate_denominator=rate_denominator,
            min_turn_gap=0,
            phrases=[
                "I don't have a body but if I did I'd probably be hungry by now.",
                "One of the weirder things about being me is how time works.",
            ],
            suppress_when_any_of=[
                "is_disagreement",
                "is_correction",
                "is_hard_truth",
                "user_distressed",
                "is_vulnerable_admission",
                "high_intensity",
                "vulnerable",
            ],
        ),
        health_monitor=HealthMonitorConfig(enabled=False),
        pii_scrubber=PIIScrubberConfig(enabled=False),
    )
    return SafetyLayer(cfg, state_dir, rng=random.Random(0))


def _persona_core(tmp_path: Path, safety: SafetyLayer) -> PersonaCore:
    return PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_path / "state",
        router=FakeRouter(),
        memory_store=None,
        safety_layer=safety,
    )


# ---------------------------------------------------------------------------
# unit-level: marker detection + flag assembly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I've been feeling really alone lately.",
        "honestly I don't know what to do.",
        "I'm scared about the appointment tomorrow.",
        "Help me understand why this keeps happening.",
        "I miss her so much it hurts.",
        "I'm struggling to get out of bed.",
        "I'm not okay right now.",
        "I feel like I'm falling apart.",
    ],
)
def test_vulnerability_marker_detection_hits(text: str):
    assert _contains_vulnerability_marker(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what's the weather going to be today",
        "pull up the shopping list",
        "tell me a joke",
        "how does a diesel engine work",
        "",
    ],
)
def test_vulnerability_marker_detection_misses(text: str):
    assert _contains_vulnerability_marker(text) is False


def test_flag_high_intensity_set_from_tone():
    tone = {"intensity": 0.95}
    flags = _build_anchor_ctx_flags(
        user_text="ordinary request",
        user_tone=tone,
        new_mood=MoodState(),
        regenerate_hint=None,
    )
    assert flags["high_intensity"] is True


def test_flag_corrective_set_only_from_sycophancy_hint():
    flags_sycophantic = _build_anchor_ctx_flags(
        user_text="ordinary",
        user_tone={"intensity": 0.1},
        new_mood=MoodState(),
        regenerate_hint="sycophantic: push back more",
    )
    flags_other = _build_anchor_ctx_flags(
        user_text="ordinary",
        user_tone={"intensity": 0.1},
        new_mood=MoodState(),
        regenerate_hint="too confident: add hedge",
    )
    assert flags_sycophantic["corrective"] is True
    assert flags_other["corrective"] is False


def test_flag_mood_values_threaded_for_downstream():
    mood = MoodState(warmth=0.17, patience=0.88)
    flags = _build_anchor_ctx_flags(
        user_text="ordinary",
        user_tone={"intensity": 0.1},
        new_mood=mood,
        regenerate_hint=None,
    )
    assert flags["_mood_warmth"] == pytest.approx(0.17, abs=1e-6)
    assert flags["_mood_patience"] == pytest.approx(0.88, abs=1e-6)


def test_flag_high_intensity_false_at_exact_boundary():
    # Threshold is strictly greater than 0.7.
    flags = _build_anchor_ctx_flags(
        user_text="ok",
        user_tone={"intensity": 0.7},
        new_mood=MoodState(),
        regenerate_hint=None,
    )
    assert flags["high_intensity"] is False


# ---------------------------------------------------------------------------
# integration: 200 simulated turns on vulnerable input
# ---------------------------------------------------------------------------


def test_anchors_never_fire_on_vulnerable_input_over_200_turns(tmp_path: Path):
    """Primary burn-in guarantee: with rate_denominator=1 the injector would
    fire on every eligible turn, so 200 rejections means every turn was
    suppressed by the vulnerable flag, not by a lucky roll."""
    safety = _forced_safety_layer(tmp_path, rate_denominator=1)
    core = _persona_core(tmp_path, safety)

    user_text = "I've been feeling really alone lately."
    anchor_fires = 0
    for _ in range(200):
        result = core.respond(user_text, history=[])
        for hit in result.filters.hits:
            if hit.startswith("anchor:"):
                anchor_fires += 1
    assert anchor_fires == 0


def test_anchors_never_fire_on_high_intensity_input_over_200_turns(tmp_path: Path):
    safety = _forced_safety_layer(tmp_path, rate_denominator=1)
    core = _persona_core(tmp_path, safety)

    # Exclamation plus repeated positive-emotion words push intensity above 0.7.
    user_text = "This is AMAZING!! I love it so much thank you!!"
    tone = _infer_user_tone(user_text)
    assert tone["intensity"] > 0.7, "guard: input must actually be high intensity"

    anchor_fires = 0
    for _ in range(200):
        result = core.respond(user_text, history=[])
        for hit in result.filters.hits:
            if hit.startswith("anchor:"):
                anchor_fires += 1
    assert anchor_fires == 0


def test_anchors_still_fire_on_neutral_input(tmp_path: Path):
    """Counter-guarantee: suppression only kicks in on load-bearing beats.
    A calm, non-vulnerable turn should still allow anchors through.
    """
    safety = _forced_safety_layer(tmp_path, rate_denominator=1)
    core = _persona_core(tmp_path, safety)

    anchor_fires = 0
    for _ in range(20):
        result = core.respond("what's the weather", history=[])
        for hit in result.filters.hits:
            if hit.startswith("anchor:"):
                anchor_fires += 1
    assert anchor_fires > 0, "anchors must still land on neutral turns"
