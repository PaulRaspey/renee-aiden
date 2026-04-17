from pathlib import Path

from src.persona.mood import MoodStore, MoodState, _circadian_energy_multiplier
from src.persona.persona_def import load_persona

ROOT = Path(__file__).resolve().parents[1]


def test_mood_load_and_persist(tmp_path: Path):
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    store = MoodStore(persona, tmp_path)
    mood = store.load()
    assert 0 <= mood.energy <= 1
    assert 0 <= mood.warmth <= 1

    mood.energy = 0.3
    store.save(mood, event="test")
    mood2 = store.load()
    assert abs(mood2.energy - 0.3) < 1e-6


def test_tone_updates_patience_down(tmp_path: Path):
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    store = MoodStore(persona, tmp_path)
    mood = store.load()
    before = mood.patience
    tone = {"valence": -0.8, "intensity": 0.9, "disagreement": 0.9, "warmth": 0.1}
    new_mood = store.apply_tone(mood, tone)
    assert new_mood.patience < before
    assert new_mood.warmth <= mood.warmth


def test_circadian_oscillates():
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    from datetime import datetime
    low = _circadian_energy_multiplier(persona, datetime(2026, 1, 1, 3, 0))
    high = _circadian_energy_multiplier(persona, datetime(2026, 1, 1, 12, 0))
    assert high > low
