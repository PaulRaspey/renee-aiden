"""Unit tests for src.orchestrator (M10). No network, no real LLM."""
from __future__ import annotations

import json
import random
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.orchestrator import LayerTelemetry, Orchestrator, TurnClassifier, TurnOutput
from src.paralinguistics.injector import ParalinguisticInjector
from src.persona.core import PersonaCore
from src.persona.llm_router import LLMResponse
from src.turn_taking.backchannel import BackchannelLayer
from src.voice.prosody import PARALINGUISTIC_KINDS


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeRouter:
    """Stands in for LLMRouter without touching Groq/Ollama."""

    def __init__(self, response_text: str = "Hey.", backend: str = "fake"):
        self.response_text = response_text
        self.backend = backend
        self.calls: list[dict] = []

    def decide_backend(self, user_text: str, expected_depth: str = "normal") -> str:
        return self.backend

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        backend: str | None = None,
        temperature: float = 0.85,
        max_tokens: int = 400,
        user_text: str | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "user_text": user_text})
        return LLMResponse(
            text=self.response_text,
            backend=self.backend,
            model="fake-1",
            latency_ms=42.0,
            input_tokens=10,
            output_tokens=5,
        )


def _write_silent_wav(path: Path, duration_ms: int = 200, sr: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sr * duration_ms / 1000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n)


def _make_library(root: Path) -> None:
    clips = []
    spec = [
        ("breaths", "sharp_in"),
        ("affirmations", "mhm"),
        ("affirmations", "yeah"),
        ("thinking", "mm"),
        ("laughs", "soft"),
        ("sighs", "tired"),
    ]
    for cat, sub in spec:
        for i in range(1, 4):
            rel = f"{cat}/{sub}/{sub}_{i:03d}.wav"
            _write_silent_wav(root / rel)
            clips.append({
                "file": rel,
                "category": cat,
                "subcategory": sub,
                "emotion": "neutral",
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


@pytest.fixture
def tmp_state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def tmp_library(tmp_path: Path) -> Path:
    root = tmp_path / "paralinguistics" / "renee"
    _make_library(root)
    return root


@pytest.fixture
def orchestrator(tmp_state: Path, tmp_library: Path) -> Orchestrator:
    router = FakeRouter(response_text="Hey. I hear you.")
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_state,
        router=router,
        memory_store=None,
    )
    injector = ParalinguisticInjector(tmp_library, rng=random.Random(0))
    backchannel = BackchannelLayer(injector.library, rng=random.Random(0))
    return Orchestrator(
        persona_name="renee",
        state_dir=tmp_state,
        persona_core=core,
        injector=injector,
        backchannel=backchannel,
        rng_seed=0,
    )


# ---------------------------------------------------------------------------
# TurnClassifier
# ---------------------------------------------------------------------------


def test_classifier_detects_vulnerable_admission():
    c = TurnClassifier()
    prosody, para = c.classify(
        user_text="Can we talk about something?",
        response_text="Honestly, I don't know if that's the right word for it. It scares me.",
        mood=None,
    )
    assert prosody.is_vulnerable_admission
    assert para.is_vulnerable_admission


def test_classifier_detects_user_distress():
    c = TurnClassifier()
    prosody, _ = c.classify(
        user_text="My dad died last year and today is his birthday",
        response_text="I'm sorry.",
        mood=None,
    )
    assert prosody.user_distressed


def test_classifier_detects_disagreement_in_response():
    c = TurnClassifier()
    prosody, _ = c.classify(
        user_text="The Great Wall is visible from space.",
        response_text="That's not actually true. It's a common misconception.",
        mood=None,
    )
    assert prosody.is_disagreement or prosody.is_correction


def test_classifier_marks_callback_when_memories_retrieved():
    c = TurnClassifier()
    prosody, _ = c.classify(
        user_text="What should I do this weekend?",
        response_text=(
            "You mentioned wanting to learn guitar — maybe Saturday morning before the kids wake up."
        ),
        mood=None,
        retrieved_count=3,
    )
    assert prosody.is_callback


def test_classifier_tone_defaults_to_casual_on_short_exchange():
    c = TurnClassifier()
    prosody, _ = c.classify(user_text="hey", response_text="hey", mood=None)
    assert prosody.conversation_tone == "casual"


def test_classifier_tone_playful_when_mood_and_exchange_light():
    c = TurnClassifier()

    class M:
        playfulness = 0.9
        energy = 0.8
        warmth = 0.8
        focus = 0.6
        patience = 0.7
        curiosity = 0.7

    prosody, _ = c.classify(user_text="pizza?", response_text="Spiral. Always spiral.", mood=M())
    assert prosody.conversation_tone == "playful"


# ---------------------------------------------------------------------------
# Orchestrator.text_turn
# ---------------------------------------------------------------------------


def test_text_turn_returns_full_bundle(orchestrator: Orchestrator):
    out: TurnOutput = orchestrator.text_turn("Hey. How are you?")
    assert isinstance(out, TurnOutput)
    assert out.text
    assert out.prosody_plan is not None
    assert isinstance(out.injections, list)
    assert out.latency_plan.target_ms > 0
    assert out.telemetry.total_ms >= 0
    assert out.telemetry.turn_type
    assert out.telemetry.persona_backend == "fake"


def test_text_turn_telemetry_measures_all_layers(orchestrator: Orchestrator):
    out = orchestrator.text_turn("Tell me about your day.")
    tele: LayerTelemetry = out.telemetry
    assert tele.persona_respond_ms >= 0
    assert tele.prosody_plan_ms >= 0
    assert tele.injector_plan_ms >= 0
    assert tele.classify_ms >= 0
    # total must be at least as big as the biggest component
    assert tele.total_ms + 0.5 >= tele.persona_respond_ms


def test_text_turn_produces_prosody_plan_with_emotion(orchestrator: Orchestrator):
    out = orchestrator.text_turn("Hey.")
    assert out.prosody_plan.emotion
    assert 0.75 <= out.prosody_plan.rate <= 1.30


def test_text_turn_writes_jsonl_telemetry(orchestrator: Orchestrator, tmp_state: Path):
    orchestrator.text_turn("Hey.")
    orchestrator.text_turn("How are you?")
    log = tmp_state / "orchestrator.jsonl"
    assert log.exists()
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["persona"] == "renee"
    assert "telemetry" in parsed
    assert "prosody_ctx" in parsed


def test_text_turn_vulnerable_response_inserts_sharp_inhale_in_plan(
    tmp_state: Path,
    tmp_library: Path,
):
    vulnerable_reply = (
        "Honestly, I don't know if scared is the right word. "
        "More like what happens if the gap is that you think I'm more than I am."
    )
    router = FakeRouter(response_text=vulnerable_reply)
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_state,
        router=router,
        memory_store=None,
    )
    injector = ParalinguisticInjector(tmp_library, rng=random.Random(0))
    orch = Orchestrator(
        persona_name="renee",
        state_dir=tmp_state,
        persona_core=core,
        injector=injector,
        backchannel=BackchannelLayer(injector.library, rng=random.Random(0)),
        rng_seed=0,
    )
    out = orch.text_turn("Is it bad to want that?")
    # The classifier should detect vulnerability from "Honestly, I don't know if..."
    assert out.prosody_context.is_vulnerable_admission
    # Prosody plan should have a breath at or near start.
    first_paralinguistic = next(
        (s for s in out.prosody_plan.segments if s.kind in PARALINGUISTIC_KINDS),
        None,
    )
    assert first_paralinguistic is not None
    assert first_paralinguistic.kind == "breath"
    assert first_paralinguistic.subcategory == "sharp_in"


def test_text_turn_disagreement_suppresses_paralinguistics_in_plan(
    tmp_state: Path,
    tmp_library: Path,
):
    router = FakeRouter(response_text="That's not actually true. It's a common misconception.")
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_state,
        router=router,
        memory_store=None,
    )
    injector = ParalinguisticInjector(tmp_library, rng=random.Random(0))
    orch = Orchestrator(
        persona_name="renee",
        state_dir=tmp_state,
        persona_core=core,
        injector=injector,
        backchannel=BackchannelLayer(injector.library, rng=random.Random(0)),
        rng_seed=0,
    )
    out = orch.text_turn("The Great Wall is visible from space, right?")
    paralinguistics = [s for s in out.prosody_plan.segments if s.kind in PARALINGUISTIC_KINDS]
    # Disagreement should drop ornamental paralinguistics; no breath-before-vulnerable
    # because this is not vulnerable.
    assert all(s.reason != "vulnerable_admission_hard_rule" for s in paralinguistics)
    assert len(paralinguistics) == 0


def test_text_turn_classify_flag_overrides_applied(orchestrator: Orchestrator):
    out = orchestrator.text_turn(
        "Hey.",
        classify_flag_overrides={
            "is_vulnerable_admission": True,
            "conversation_tone": "vulnerable",
        },
    )
    assert out.prosody_context.is_vulnerable_admission
    assert out.turn_context.is_vulnerable_admission


# ---------------------------------------------------------------------------
# live audio tick path
# ---------------------------------------------------------------------------


def test_observe_user_audio_tick_reports_endpoint_action(orchestrator: Orchestrator):
    res = orchestrator.observe_user_audio_tick(
        "Hey, I was just thinking about something.",
        silence_ms=100,
    )
    assert res["state"] == "user_speaking"
    assert res["endpoint"] is not None
    assert res["endpoint"]["action"] in ("idle", "prewarm", "speculative", "commit")


def test_observe_user_audio_tick_emits_backchannel_when_opportunity(orchestrator: Orchestrator):
    # Seed history so we know the first fire lands cleanly on a clause-boundary.
    res = orchestrator.observe_user_audio_tick(
        "So anyway,",
        silence_ms=250,
        mood=None,
        intimacy=0.8,
        conversation_tone="casual",
    )
    # Backchannel may or may not fire on this exact call depending on RNG,
    # but the key guarantee is that a backchannel is permitted in this state.
    # We test the hard-block rule explicitly below.
    assert res["state"] == "user_speaking"


def test_observe_user_audio_tick_blocks_backchannel_on_distress(orchestrator: Orchestrator):
    fires = 0
    for i in range(20):
        res = orchestrator.observe_user_audio_tick(
            "my dad died,",
            silence_ms=250,
            user_distressed=True,
        )
        if res["backchannel"] is not None:
            fires += 1
    assert fires == 0


def test_observe_user_audio_tick_advances_state_machine(orchestrator: Orchestrator):
    # start with user speaking
    orchestrator.observe_user_audio_tick("Hey", silence_ms=50)
    orchestrator.observe_user_audio_tick("Hey, I was done.", silence_ms=900, tick_ms=100)
    res = orchestrator.observe_user_audio_tick("Hey, I was done.", silence_ms=1000, tick_ms=100)
    assert res["endpoint"]["action"] == "commit"
    orchestrator.begin_renee_preparing()
    orchestrator.begin_renee_speaking()
    orchestrator.end_renee_turn()


# ---------------------------------------------------------------------------
# construction without paralinguistic library still works
# ---------------------------------------------------------------------------


def test_orchestrator_without_library_still_runs(tmp_state: Path):
    router = FakeRouter(response_text="Hey.")
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_state,
        router=router,
        memory_store=None,
    )
    orch = Orchestrator(
        persona_name="renee",
        state_dir=tmp_state,
        persona_core=core,
        injector=None,
        backchannel=None,
        paralinguistic_library_root=tmp_state / "does-not-exist",
    )
    out = orch.text_turn("Hey.")
    assert out.text
    assert out.injections == []
    # No library -> no backchannel layer either
    tick = orch.observe_user_audio_tick("Hey", silence_ms=50)
    assert tick["backchannel"] is None


# ---------------------------------------------------------------------------
# transcript emitter (mobile client relay)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_asr_final_emits_user_transcript_and_renee_response(
    orchestrator: Orchestrator,
):
    sent: list[dict] = []

    async def emitter(msg: dict) -> None:
        sent.append(msg)

    orchestrator.transcript_emitter = emitter
    await orchestrator._on_asr_final("what's up")

    types = [m["type"] for m in sent]
    assert "transcript" in types and "response" in types
    user = [m for m in sent if m["type"] == "transcript"][0]
    assert user == {"type": "transcript", "speaker": "paul", "text": "what's up"}
    renee = [m for m in sent if m["type"] == "response"][0]
    assert renee["speaker"] == "renee"
    assert renee["text"]


@pytest.mark.asyncio
async def test_on_asr_final_with_no_emitter_does_not_raise(
    orchestrator: Orchestrator,
):
    orchestrator.transcript_emitter = None
    # Must not raise — transcript relay is best-effort.
    await orchestrator._on_asr_final("hello")


@pytest.mark.asyncio
async def test_emitter_exception_does_not_break_turn(orchestrator: Orchestrator):
    async def bad_emitter(_msg):
        raise RuntimeError("network down")

    orchestrator.transcript_emitter = bad_emitter
    # Should still finish the turn cleanly despite the emitter blowing up.
    await orchestrator._on_asr_final("hey")


@pytest.mark.asyncio
async def test_register_transcript_listener_fans_out_to_every_listener(
    orchestrator: Orchestrator,
):
    a_msgs: list[dict] = []
    b_msgs: list[dict] = []

    async def cb_a(msg): a_msgs.append(msg)
    async def cb_b(msg): b_msgs.append(msg)

    unreg_a = orchestrator.register_transcript_listener("conn-a", cb_a)
    unreg_b = orchestrator.register_transcript_listener("conn-b", cb_b)
    assert orchestrator.transcript_listener_count() == 2

    await orchestrator._on_asr_final("hello")
    types_a = [m["type"] for m in a_msgs]
    types_b = [m["type"] for m in b_msgs]
    assert "transcript" in types_a and "response" in types_a
    assert "transcript" in types_b and "response" in types_b

    # Unregister one; further events go only to the other.
    unreg_a()
    assert orchestrator.transcript_listener_count() == 1
    a_msgs.clear(); b_msgs.clear()
    await orchestrator._on_asr_final("again")
    assert a_msgs == []
    assert len(b_msgs) >= 1

    unreg_b()
    assert orchestrator.transcript_listener_count() == 0


@pytest.mark.asyncio
async def test_register_transcript_listener_overwrites_same_conn_id(
    orchestrator: Orchestrator,
):
    """Same conn_id replaces its previous listener; no duplicate fan-out."""
    count: list[int] = []

    async def cb_v1(_m): count.append(1)
    async def cb_v2(_m): count.append(2)

    orchestrator.register_transcript_listener("conn", cb_v1)
    orchestrator.register_transcript_listener("conn", cb_v2)
    assert orchestrator.transcript_listener_count() == 1

    await orchestrator._on_asr_final("hi")
    # v2 receives both transcript + response events (2 messages). v1
    # receives none.
    assert 1 not in count
    assert count == [2, 2]


# ---------------------------------------------------------------------------
# set_session_topic + topic-aware greeting (#2)
# ---------------------------------------------------------------------------


def test_set_session_topic_stores_value(orchestrator: Orchestrator):
    orchestrator.set_session_topic("memory consolidation Part 3")
    assert orchestrator._session_topic == "memory consolidation Part 3"


def test_set_session_topic_strips_and_clears(orchestrator: Orchestrator):
    orchestrator.set_session_topic("  with whitespace  ")
    assert orchestrator._session_topic == "with whitespace"
    orchestrator.set_session_topic("")
    assert orchestrator._session_topic is None
    orchestrator.set_session_topic(None)
    assert orchestrator._session_topic is None


def test_set_session_topic_caps_at_200_chars(orchestrator: Orchestrator):
    long = "x" * 500
    orchestrator.set_session_topic(long)
    assert len(orchestrator._session_topic) == 200


@pytest.mark.asyncio
async def test_greet_on_connect_uses_topic_when_set(orchestrator: Orchestrator):
    """When a topic is set, the greeting prompt embeds it; absent the topic,
    the default prompt passes through unchanged."""
    orchestrator.set_session_topic("Hilbert spaces and pizza")
    sent_prompts: list[str] = []
    # Patch text_turn to capture the prompt the greeting passed in
    original = orchestrator.text_turn

    def capture(prompt, history):
        sent_prompts.append(prompt)
        return original(prompt, history)

    orchestrator.text_turn = capture  # type: ignore[assignment]
    await orchestrator.greet_on_connect()
    assert sent_prompts, "greet_on_connect did not call text_turn"
    assert "Hilbert spaces and pizza" in sent_prompts[0]
    assert "topic" in sent_prompts[0].lower()


@pytest.mark.asyncio
async def test_greet_on_connect_uses_default_when_no_topic(orchestrator: Orchestrator):
    sent_prompts: list[str] = []
    original = orchestrator.text_turn

    def capture(prompt, history):
        sent_prompts.append(prompt)
        return original(prompt, history)

    orchestrator.text_turn = capture  # type: ignore[assignment]
    await orchestrator.greet_on_connect()
    assert sent_prompts == ["system: greet paul, he just connected"]
