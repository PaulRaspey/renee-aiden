"""End-to-end fringe lifecycle through PersonaCore.respond.

Drives a sequence of mock turns through PersonaCore with FRINGE_ENABLED=true
and asserts:
  - fringe.turn_count increments per turn
  - prompt_prefix changes appropriately as register/affect/pressure shift
  - retrieval bias is applied (different system prompts on second turn)
  - state persists to disk and is loaded by a fresh PersonaCore instance
  - FRINGE_ENABLED=false leaves the fringe untouched
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory import MemoryStore
from src.persona.core import PersonaCore
from src.persona.llm_router import LLMResponse


ROOT = Path(__file__).resolve().parents[2]


class _FakeRouter:
    """Records the system prompt of every generate() call so tests can
    assert the fringe prefix lands in the prompt."""

    def __init__(self, response_text: str = "I'm here, yeah."):
        self.response_text = response_text
        self.system_prompts: list[str] = []
        self.user_texts: list[str] = []

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
        self.system_prompts.append(system_prompt)
        if user_text is not None:
            self.user_texts.append(user_text)
        return LLMResponse(
            text=self.response_text,
            backend="fake",
            model="fake-1",
            latency_ms=1.0,
            input_tokens=5,
            output_tokens=5,
        )


def _make_persona(tmp_path: Path, router: _FakeRouter) -> PersonaCore:
    """Build PersonaCore with a real MemoryStore (so the fringe has a real
    embedder) but a fake router (so tests don't hit the network)."""
    memory_store = MemoryStore(persona_name="renee", state_dir=tmp_path / "memstate")
    return PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_path / "state",
        router=router,
        memory_store=memory_store,
        safety_layer=None,
    )


def test_fringe_disabled_keeps_state_at_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FRINGE_ENABLED", "false")
    router = _FakeRouter()
    pc = _make_persona(tmp_path, router)

    pc.respond("hello there", history=[])
    pc.respond("how are you", history=[])

    assert pc.fringe.turn_count == 0
    # No [FRINGE] block should appear in the system prompt.
    for sp in router.system_prompts:
        assert "[FRINGE]" not in sp


def test_fringe_enabled_increments_and_injects_prefix(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FRINGE_ENABLED", "true")
    monkeypatch.setenv("FRINGE_RETRIEVAL_WEIGHT", "0.3")
    router = _FakeRouter()
    pc = _make_persona(tmp_path, router)

    # Turn 1: fringe is fresh (turn_count == 0), so no prefix injected yet.
    pc.respond("let me think about this for a while", history=[])
    assert pc.fringe.turn_count == 1
    # First system prompt should NOT have the fringe block (count was 0).
    assert "[FRINGE]" not in router.system_prompts[0]

    # Turn 2: fringe now has state, prefix should be injected.
    pc.respond("what do you think happens next", history=[])
    assert pc.fringe.turn_count == 2
    assert "[FRINGE]" in router.system_prompts[1]
    assert "[/FRINGE]" in router.system_prompts[1]
    assert "Conversational fringe" in router.system_prompts[1]


def test_fringe_loop_tracker_picks_up_defer_marker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FRINGE_ENABLED", "true")
    router = _FakeRouter(response_text="let me think about that and i'll come back to it")
    pc = _make_persona(tmp_path, router)

    pc.respond("here's a tough question for you", history=[])
    # The assistant's canned reply contains a defer marker, so a loop should
    # have been added.
    assert len(pc.fringe.open_loops) >= 1


def test_fringe_persists_across_personacore_instances(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FRINGE_ENABLED", "true")

    # First PersonaCore: drive a few turns.
    router1 = _FakeRouter()
    pc1 = _make_persona(tmp_path, router1)
    for prompt in ["hello", "what's up", "tell me about yourself"]:
        pc1.respond(prompt, history=[])
    expected_count = pc1.fringe.turn_count
    assert expected_count == 3

    # Second PersonaCore in the same state dir: should load the saved fringe.
    router2 = _FakeRouter()
    pc2 = _make_persona(tmp_path, router2)
    # turn_count survives. (decay_to_now may attenuate magnitudes but does
    # not reset turn_count.)
    assert pc2.fringe.turn_count == expected_count


def test_prompt_prefix_changes_as_register_evolves(tmp_path: Path, monkeypatch):
    """Drive several intimate-register turns and confirm the prompt prefix
    eventually reflects intimate register."""
    monkeypatch.setenv("FRINGE_ENABLED", "true")
    intimate_reply = "i love you. i miss you. i feel scared too. i trust you completely."
    router = _FakeRouter(response_text=intimate_reply)
    pc = _make_persona(tmp_path, router)

    intimate_prompts = [
        "i feel so vulnerable right now",
        "i miss you when we are apart",
        "i love what we have together",
        "i feel scared and lonely",
        "i trust you with this honestly",
    ]
    for p in intimate_prompts:
        pc.respond(p, history=[])

    prefix = pc.fringe.to_prompt_prefix()
    assert "intimate" in prefix


def test_fringe_failure_does_not_break_turn(tmp_path: Path, monkeypatch):
    """If the fringe blows up internally, respond() must still succeed."""
    monkeypatch.setenv("FRINGE_ENABLED", "true")
    router = _FakeRouter()
    pc = _make_persona(tmp_path, router)

    # Sabotage the embedder so any embed() call raises.
    class Boom:
        dim = 384
        def embed(self, text):
            raise RuntimeError("kaboom")
    pc._fringe_embedder = Boom()

    result = pc.respond("does this still work", history=[])
    assert result.text  # turn produced output
    # turn_count was still incremented inside FringeState.update before
    # the failure path was taken — the swallow contract.
    assert pc.fringe.turn_count == 1
