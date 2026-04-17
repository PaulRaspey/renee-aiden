"""
Unit tests for src.paralinguistics.injector.

All tests use a synthetic library built in a tmp_path: tiny silent WAVs plus a
metadata.yaml. No real audio or ElevenLabs calls.
"""
from __future__ import annotations

import random
import wave
from pathlib import Path

import pytest
import yaml

from src.paralinguistics import (
    Injection,
    MoodLike,
    ParalinguisticInjector,
    POSITION_END,
    POSITION_START,
    TurnContext,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

SYNTHETIC_CLIPS = [
    # (cat, sub, emotion, intensity, energy, tags, inappropriate)
    ("laughs", "soft", "amused", 0.3, 0.5, ["warm", "casual"], ["serious_discussion"]),
    ("laughs", "soft", "amused", 0.5, 0.6, ["warm", "casual"], ["serious_discussion"]),
    ("laughs", "hearty", "delighted", 0.8, 0.9, ["high_energy"], ["quiet_moment"]),
    ("sighs", "frustrated", "frustrated", 0.6, 0.65, ["frustrated"], ["agreement"]),
    ("sighs", "tired", "tired", 0.4, 0.2, ["low_energy"], ["celebration"]),
    ("breaths", "sharp_in", "bracing", 0.4, 0.6, ["pre_admission"], ["casual_banter"]),
    ("thinking", "mm", "acknowledging", 0.3, 0.4, ["listening"], ["confrontation"]),
    ("thinking", "hmm", "considering", 0.35, 0.4, ["pause"], ["fast_agreement"]),
    ("reactions", "amusement", "amused", 0.4, 0.7, ["amused"], ["serious_moment"]),
    ("affirmations", "yeah", "agreeing", 0.35, 0.55, ["agreement"], ["disagreement"]),
]


def _write_silent_wav(path: Path, duration_ms: int = 200, sr: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(sr * duration_ms / 1000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n_samples)


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    root = tmp_path / "paralinguistics" / "renee"
    clips = []
    for idx, (cat, sub, emotion, intensity, energy, tags, inappropriate) in enumerate(SYNTHETIC_CLIPS, start=1):
        rel = f"{cat}/{sub}/{sub}_{idx:03d}.wav"
        _write_silent_wav(root / rel)
        clips.append({
            "file": rel,
            "category": cat,
            "subcategory": sub,
            "emotion": emotion,
            "intensity": intensity,
            "energy_level": energy,
            "tags": tags,
            "appropriate_contexts": [],
            "inappropriate_contexts": inappropriate,
            "duration_ms": 200,
            "sample_rate": 24000,
        })
    (root / "metadata.yaml").write_text(yaml.safe_dump({"voice": "renee", "clips": clips}), encoding="utf-8")
    return root


@pytest.fixture
def injector(library_root: Path) -> ParalinguisticInjector:
    return ParalinguisticInjector(library_root, rng=random.Random(42))


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------

def test_library_loads_all_clips(injector):
    assert injector.library_size() == len(SYNTHETIC_CLIPS)
    assert len(injector.library.get("laughs", "soft")) == 2


def test_library_skips_missing_files(tmp_path):
    root = tmp_path / "paralinguistics" / "renee"
    root.mkdir(parents=True)
    (root / "metadata.yaml").write_text(
        yaml.safe_dump({
            "voice": "renee",
            "clips": [{
                "file": "laughs/soft/does_not_exist.wav",
                "category": "laughs", "subcategory": "soft",
            }],
        }),
        encoding="utf-8",
    )
    inj = ParalinguisticInjector(root)
    assert inj.library_size() == 0


# ---------------------------------------------------------------------------
# hard rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ctx_attr", [
    "is_disagreement", "is_correction", "is_hard_truth", "user_distressed",
])
def test_hard_rule_blocks_all_paralinguistics(injector, ctx_attr):
    mood = MoodLike(energy=0.7, playfulness=0.9)
    ctx = TurnContext(**{ctx_attr: True, "is_vulnerable_admission": True, "is_witty_callback": True})
    assert injector.plan("I hear you.", mood, ctx) == []


def test_heated_tone_blocks(injector):
    ctx = TurnContext(conversation_tone="heated", is_vulnerable_admission=True)
    assert injector.plan("Listen.", MoodLike(), ctx) == []


# ---------------------------------------------------------------------------
# proposal rules
# ---------------------------------------------------------------------------

def test_vulnerable_admission_produces_sharp_inhale(injector):
    mood = MoodLike(energy=0.6)
    ctx = TurnContext(is_vulnerable_admission=True, conversation_tone="vulnerable")
    injector.rng = random.Random(0)  # force density pass
    plan = injector.plan("I don't always know what I am.", mood, ctx)
    cats = {(i.category, i.subcategory) for i in plan}
    assert ("breaths", "sharp_in") in cats
    first_breath = next(i for i in plan if i.category == "breaths")
    assert first_breath.position == POSITION_START


def test_witty_callback_with_playful_mood_adds_soft_laugh(injector):
    mood = MoodLike(playfulness=0.8, energy=0.7)
    ctx = TurnContext(is_witty_callback=True, conversation_tone="playful")
    plan = injector.plan("That's the fourth time you've done that this week.", mood, ctx)
    laughs = [i for i in plan if i.category == "laughs"]
    assert laughs
    assert laughs[0].position == POSITION_END


def test_high_complexity_injects_thinking_pause(injector):
    mood = MoodLike(energy=0.6)
    ctx = TurnContext(turn_complexity=0.85, conversation_tone="serious")
    # serious tone has density 0.15 — force the density check to pass
    injector.rng = random.Random(1)
    for _ in range(5):
        plan = injector.plan("Let me walk through this.", mood, ctx)
        if plan:
            break
    thinking = [i for i in plan if i.category == "thinking"]
    assert thinking, "expected at least one thinking-pause injection"


def test_repeated_confusion_with_low_patience_triggers_frustrated_sigh(injector):
    mood = MoodLike(patience=0.2, energy=0.6)
    ctx = TurnContext(user_confused_repeatedly=True, conversation_tone="casual")
    injector.rng = random.Random(2)
    plan = injector.plan("Okay let me try this one more time.", mood, ctx)
    sighs = [i for i in plan if i.category == "sighs" and i.subcategory == "frustrated"]
    assert sighs


# ---------------------------------------------------------------------------
# constraints
# ---------------------------------------------------------------------------

def test_frequency_cap_enforced(injector):
    mood = MoodLike(playfulness=0.9, energy=0.2, patience=0.2)
    ctx = TurnContext(
        is_vulnerable_admission=True,
        is_witty_callback=True,
        turn_complexity=0.9,
        user_confused_repeatedly=True,
        conversation_tone="playful",
    )
    injector.rng = random.Random(3)
    plan = injector.plan("This is a long response with multiple signals in it.", mood, ctx)
    assert len(plan) <= injector.max_per_turn


def test_deduplication_by_category_subcategory(injector):
    mood = MoodLike(playfulness=0.8, energy=0.3)
    ctx = TurnContext(
        is_vulnerable_admission=True,  # adds breaths/sharp_in
        turn_complexity=0.8,           # adds thinking/mm
        conversation_tone="vulnerable",
    )
    injector.rng = random.Random(4)
    plan = injector.plan("Can I tell you something hard?", mood, ctx)
    keys = [(i.category, i.subcategory) for i in plan]
    assert len(keys) == len(set(keys))


def test_empty_text_returns_empty(injector):
    assert injector.plan("", MoodLike(), TurnContext()) == []


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def test_clip_is_bound_to_injection(injector):
    mood = MoodLike(playfulness=0.85, energy=0.7)
    ctx = TurnContext(is_witty_callback=True, conversation_tone="playful")
    injector.rng = random.Random(5)
    plan = injector.plan("You would say that.", mood, ctx)
    assert plan
    for inj in plan:
        assert inj.clip_path is not None
        assert inj.clip_path.exists()


def test_recent_clip_avoidance(injector):
    """After a clip is played, the selector should prefer another one next time."""
    mood = MoodLike(playfulness=0.9, energy=0.7)
    ctx = TurnContext(is_witty_callback=True, conversation_tone="playful")

    first = None
    seen_different = False
    for i in range(8):
        injector.rng = random.Random(100 + i)
        plan = injector.plan(f"Turn {i}", mood, ctx)
        laugh_clips = [inj for inj in plan if inj.category == "laughs"]
        if not laugh_clips:
            continue
        chosen = str(laugh_clips[0].clip_path)
        if first is None:
            first = chosen
        elif chosen != first:
            seen_different = True
            break
    assert seen_different, "selector never diversified laughs/soft over 8 turns"


def test_low_energy_rejects_high_energy_clips(injector):
    """mood.energy very low -> hearty laugh downshifts to soft."""
    mood = MoodLike(playfulness=0.8, energy=0.2)
    ctx = TurnContext(
        is_witty_callback=True,
        conversation_tone="playful",
    )
    injector.rng = random.Random(6)
    plan = injector.plan("...", mood, ctx)
    for inj in plan:
        if inj.category == "laughs":
            assert inj.subcategory == "soft"  # downshifted from hearty by mood filter


def test_missing_subcategory_falls_back_to_category(tmp_path):
    """If we ask for sighs/content but only sighs/tired exists, bind one anyway."""
    root = tmp_path / "paralinguistics" / "renee"
    _write_silent_wav(root / "sighs/tired/tired_001.wav")
    (root / "metadata.yaml").write_text(
        yaml.safe_dump({
            "voice": "renee",
            "clips": [{
                "file": "sighs/tired/tired_001.wav",
                "category": "sighs", "subcategory": "tired",
                "emotion": "tired", "intensity": 0.3, "energy_level": 0.3,
                "tags": [], "appropriate_contexts": [], "inappropriate_contexts": [],
            }],
        }),
        encoding="utf-8",
    )
    inj = ParalinguisticInjector(root, rng=random.Random(0))
    # artificially construct an injection targeting sighs/content
    from src.paralinguistics.injector import Injection
    target = Injection(category="sighs", subcategory="content", position=0, intensity=0.3)
    bound = inj._bind_clips([target], MoodLike(), 1234567890.0)
    assert bound[0].clip_path is not None
