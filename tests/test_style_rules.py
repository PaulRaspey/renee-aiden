"""Tests for M12 style-rule loading + prompt/prosody integration."""
from __future__ import annotations

from pathlib import Path

import yaml

from src.persona.style_rules import (
    MOOD_LABEL_TO_TONE,
    StyleReference,
    load_style_reference,
)
from src.persona.prompt_assembler import build_system_prompt
from src.persona.mood import MoodState
from src.persona.persona_def import load_persona
from src.voice.prosody import ProsodyPlanner


ROOT = Path(__file__).resolve().parents[1]
STYLE_YAML = ROOT / "configs" / "style_reference.yaml"


def test_loader_returns_none_when_file_missing(tmp_path):
    assert load_style_reference(tmp_path / "nope.yaml") is None


def test_loader_parses_real_reference():
    ref = load_style_reference(STYLE_YAML)
    assert ref is not None
    assert ref.extracted_for == "renee"
    assert ref.turn_length_median > 0
    assert ref.turn_length_p95 >= ref.turn_length_median
    assert 0.0 <= ref.hedge_frequency <= 1.0
    assert ref.paralinguistics_per_turn > 0.0
    assert ref.mood_arc, "mood_arc must be populated"
    assert ref.scenes, "per-scene stats must be populated"
    # Anchors filtered of exclamations — the script deliberately introduces
    # Florence/Brunello/Marcus as callbacks, so at least one should survive.
    anchors = set(ref.callback_anchors)
    assert anchors & {"Florence", "Brunello", "Marcus", "Paul"}, anchors


def test_paralinguistic_density_by_tone_non_empty():
    ref = load_style_reference(STYLE_YAML)
    densities = ref.paralinguistic_density_by_tone()
    # At least one tone derived from the mood arc.
    assert densities
    # All values are non-negative floats.
    for tone, value in densities.items():
        assert isinstance(value, float)
        assert value >= 0.0
    # Mapped keys are a subset of prosody tone keys.
    assert set(densities.keys()) <= set(MOOD_LABEL_TO_TONE.values())


def test_prompt_style_block_has_concrete_targets():
    ref = load_style_reference(STYLE_YAML)
    block = ref.prompt_style_block()
    lower = block.lower()
    assert "turn length" in lower
    assert "hedge" in lower
    assert "paralinguistic density" in lower
    assert "false starts" in lower
    assert "signature phrases" in lower


def test_build_system_prompt_injects_style_block():
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    mood = MoodState(
        energy=0.7, warmth=0.8, playfulness=0.7,
        focus=0.7, patience=0.6, curiosity=0.8,
    )
    ref = load_style_reference(STYLE_YAML)
    prompt = build_system_prompt(persona, mood, style_reference=ref)
    assert "STYLE CONSTRAINTS" in prompt
    assert "Turn length median" in prompt


def test_build_system_prompt_omits_style_block_when_no_reference():
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    mood = MoodState(
        energy=0.7, warmth=0.8, playfulness=0.7,
        focus=0.7, patience=0.6, curiosity=0.8,
    )
    prompt = build_system_prompt(persona, mood, style_reference=None)
    assert "STYLE CONSTRAINTS" not in prompt


def test_prosody_planner_absorbs_measured_density():
    ref = load_style_reference(STYLE_YAML)
    planner = ProsodyPlanner(style_reference=ref)
    density = planner.rules["paralinguistic_density"]
    # At least one tone was overridden; the serious bucket should move to the
    # measured value (non-default 0.1) when the mood_arc has a 'serious' scene.
    # The override marker should be present.
    assert planner.rules.get("_style_overrides")
    assert "casual" in density


def test_prosody_planner_without_reference_keeps_defaults():
    planner = ProsodyPlanner()
    assert "_style_overrides" not in planner.rules
    density = planner.rules["paralinguistic_density"]
    # Defaults from DEFAULT_RULES.
    assert density["casual"] == 0.4


def test_style_reference_yaml_roundtrip_is_sane():
    # Ensure the on-disk YAML still contains everything our loader expects.
    data = yaml.safe_load(STYLE_YAML.read_text(encoding="utf-8"))
    for key in (
        "turn_length",
        "hedge_frequency_renee",
        "paralinguistics_per_turn_renee",
        "pause_distribution_renee",
        "register_markers_renee",
        "mood_arc_renee",
        "scenes_renee",
        "callbacks_renee",
        "vocabulary_texture_renee",
    ):
        assert key in data, f"missing section: {key}"
