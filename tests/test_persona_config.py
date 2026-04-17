from pathlib import Path

from src.persona.persona_def import load_persona

ROOT = Path(__file__).resolve().parents[1]


def test_load_renee():
    p = load_persona(ROOT / "configs" / "renee.yaml")
    assert p.name == "Renée"
    assert p.hedge_frequency > 0.0
    assert "as an AI" in p.never_uses


def test_load_aiden():
    p = load_persona(ROOT / "configs" / "aiden.yaml")
    assert p.name == "Aiden"
    assert p.personality["directness"] >= p.personality.get("warmth", 0.7)
