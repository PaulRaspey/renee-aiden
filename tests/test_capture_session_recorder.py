"""Tests for the session recorder + orchestrator audio tap (Feature 1).

Checks the session-directory contract, the WAV format, opt-in semantics,
the Ctrl+C path via context manager + KeyboardInterrupt, the QAL chain
genesis / continuation behaviour, and the bit-for-bit audio-tap contract
on the orchestrator.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import wave
from pathlib import Path

import pytest

from src.capture.session_recorder import (
    DEFAULT_SESSIONS_ROOT,
    SessionRecorder,
    default_sessions_root,
    generate_session_id,
    is_recording_enabled_via_env,
    write_chain_root,
)
from src.identity.uahp_identity import create_identity
from src.memory.store import MemoryStore
from src.uahp.memory_wiring import verify_memory_snapshot
from src.uahp.qal_chain import (
    GENESIS_PREV_HASH,
    load_chain,
    verify_chain,
)


SAMPLE_PCM = bytes(960 * 2)


@pytest.fixture
def memory_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(persona_name="test", state_dir=tmp_path / "state")


@pytest.fixture
def identity():
    return create_identity("renee_persona")


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    return tmp_path / "renee-sessions"


def _fixed_clock(start_second: int = 0):
    """Return a clock that increments by one second per call. Share one
    instance across recorders in a test when distinct session_ids matter."""
    counter = {"i": start_second}

    def _now():
        counter["i"] += 1
        base = _dt.datetime(2026, 4, 21, 19, 30, 0, tzinfo=_dt.timezone.utc)
        return base + _dt.timedelta(seconds=counter["i"])
    return _now


# ---------------------------------------------------------------------------
# env handling
# ---------------------------------------------------------------------------


def test_default_sessions_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("RENEE_SESSIONS_DIR", str(tmp_path / "custom"))
    assert default_sessions_root() == Path(str(tmp_path / "custom"))


def test_default_sessions_root_fallback(monkeypatch):
    monkeypatch.delenv("RENEE_SESSIONS_DIR", raising=False)
    assert default_sessions_root() == DEFAULT_SESSIONS_ROOT


def test_recording_env_flag(monkeypatch):
    monkeypatch.delenv("RENEE_RECORD", raising=False)
    assert is_recording_enabled_via_env() is False
    monkeypatch.setenv("RENEE_RECORD", "1")
    assert is_recording_enabled_via_env() is True
    monkeypatch.setenv("RENEE_RECORD", "yes")
    assert is_recording_enabled_via_env() is True
    monkeypatch.setenv("RENEE_RECORD", "0")
    assert is_recording_enabled_via_env() is False


# ---------------------------------------------------------------------------
# opt-in semantics
# ---------------------------------------------------------------------------


def test_disabled_recorder_start_noop(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=False,
    )
    assert rec.start() is None
    assert not sessions_root.exists()
    rec.on_mic_pcm(SAMPLE_PCM)
    rec.on_renee_pcm(SAMPLE_PCM)
    rec.on_transcript({"type": "transcript"})
    assert not sessions_root.exists()


def test_env_var_enables_recording(monkeypatch, memory_store, identity, sessions_root):
    monkeypatch.setenv("RENEE_RECORD", "1")
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
    )
    assert rec.enabled is True


def test_active_recording_indicator_fires(memory_store, identity, sessions_root):
    shown = []
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
        indicator_fn=lambda msg: shown.append(msg),
    )
    rec.start()
    rec.stop()
    assert any("RECORDING" in m for m in shown)
    assert any("STOPPED" in m for m in shown)


# ---------------------------------------------------------------------------
# directory + file format
# ---------------------------------------------------------------------------


def test_session_directory_structure(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
        now=_fixed_clock(),
    )
    session_dir = rec.start()
    rec.on_mic_pcm(SAMPLE_PCM)
    rec.on_renee_pcm(SAMPLE_PCM)
    rec.on_transcript({"type": "transcript", "speaker": "paul", "text": "hi"})
    rec.record_eval_score({"probe": "latency", "value": 123})
    rec.stop()
    assert (session_dir / "mic.wav").exists()
    assert (session_dir / "renee.wav").exists()
    assert (session_dir / "transcript.json").exists()
    assert (session_dir / "eval_scores.json").exists()
    assert (session_dir / "session_manifest.json").exists()
    assert (session_dir / "attestation_chain.jsonl").exists()


def test_wav_is_48k_16bit_mono(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
    )
    session_dir = rec.start()
    rec.on_mic_pcm(SAMPLE_PCM)
    rec.on_renee_pcm(SAMPLE_PCM)
    rec.stop()
    for name in ("mic.wav", "renee.wav"):
        with wave.open(str(session_dir / name), "rb") as w:
            assert w.getframerate() == 48000
            assert w.getsampwidth() == 2
            assert w.getnchannels() == 1


def test_wav_bit_for_bit_preserved(memory_store, identity, sessions_root):
    canned = bytes((i * 7) % 256 for i in range(960 * 2))
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
    )
    session_dir = rec.start()
    rec.on_mic_pcm(canned)
    rec.stop()
    with wave.open(str(session_dir / "mic.wav"), "rb") as w:
        read_back = w.readframes(w.getnframes())
    assert read_back == canned


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_manifest_schema_and_defaults(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
        starter_metadata={
            "starter_index": 3,
            "target_duration_minutes": 60,
            "curveball_planned_minute": 18,
            "curveball_actual_minute": None,
        },
        backend_used="cascade",
        pod_id="pod-abc",
        renee_versions={
            "prosody": "0.5.5",
            "persona": "0.5.5",
            "safety_layer": "0.5.5",
        },
    )
    session_dir = rec.start()
    rec.stop()
    manifest = json.loads((session_dir / "session_manifest.json").read_text())
    for key in (
        "session_id",
        "start_time",
        "end_time",
        "renee_versions",
        "backend_used",
        "pod_id",
        "starter_metadata",
        "public",
        "reviewed",
        "github_published",
        "presence_score",
        "notes_file",
        "genesis_session",
        "memory_snapshot",
    ):
        assert key in manifest
    assert manifest["public"] is False
    assert manifest["reviewed"] is False
    assert manifest["github_published"] is False
    assert manifest["presence_score"] is None
    assert manifest["backend_used"] == "cascade"
    assert manifest["pod_id"] == "pod-abc"
    assert manifest["starter_metadata"]["starter_index"] == 3
    assert manifest["renee_versions"]["prosody"] == "0.5.5"


def test_manifest_notes_file_points_into_session_dir(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
    )
    session_dir = rec.start()
    rec.stop()
    manifest = json.loads((session_dir / "session_manifest.json").read_text())
    assert Path(manifest["notes_file"]).parent == session_dir


def test_memory_snapshot_attached_and_valid(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
    )
    session_dir = rec.start()
    rec.stop()
    manifest = json.loads((session_dir / "session_manifest.json").read_text())
    assert verify_memory_snapshot(identity, manifest["memory_snapshot"]) is True


# ---------------------------------------------------------------------------
# Ctrl+C + graceful close
# ---------------------------------------------------------------------------


def test_ctrl_c_closes_wav_cleanly(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
    )
    try:
        with rec:
            rec.on_mic_pcm(SAMPLE_PCM)
            rec.on_renee_pcm(SAMPLE_PCM)
            raise KeyboardInterrupt("simulate ctrl+c")
    except KeyboardInterrupt:
        pass
    assert rec.session_dir is not None
    with wave.open(str(rec.session_dir / "mic.wav"), "rb") as w:
        assert w.getnframes() == len(SAMPLE_PCM) // 2
    with wave.open(str(rec.session_dir / "renee.wav"), "rb") as w:
        assert w.getnframes() == len(SAMPLE_PCM) // 2
    manifest = json.loads((rec.session_dir / "session_manifest.json").read_text())
    assert manifest["end_time"]


# ---------------------------------------------------------------------------
# QAL chain behaviour
# ---------------------------------------------------------------------------


def test_first_session_is_genesis(memory_store, identity, sessions_root):
    rec = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
    )
    session_dir = rec.start()
    rec.stop()
    manifest = json.loads((session_dir / "session_manifest.json").read_text())
    assert manifest["genesis_session"] is True
    chain = load_chain(session_dir / "attestation_chain.jsonl")
    assert len(chain) == 1
    assert chain[0].prev_hash == GENESIS_PREV_HASH
    root = json.loads((sessions_root / "global_chain_root.json").read_text())
    assert root["genesis_session_id"] == rec.session_id
    assert root["last_session_id"] == rec.session_id
    assert root["chain_length"] == 1
    assert root["last_attestation"]["signature"] == chain[0].signature


def test_second_session_appends_not_genesis(memory_store, identity, sessions_root):
    clock = _fixed_clock()
    rec1 = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
        now=clock,
    )
    d1 = rec1.start()
    rec1.stop()
    rec2 = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
        now=clock,
    )
    d2 = rec2.start()
    rec2.stop()
    assert d1 != d2
    manifest2 = json.loads((d2 / "session_manifest.json").read_text())
    assert manifest2["genesis_session"] is False
    chain2 = load_chain(d2 / "attestation_chain.jsonl")
    assert chain2[0].prev_hash != GENESIS_PREV_HASH
    root = json.loads((sessions_root / "global_chain_root.json").read_text())
    assert root["chain_length"] == 2
    assert root["last_session_id"] == rec2.session_id
    assert root["genesis_session_id"] == rec1.session_id


def test_bak_created_on_second_write(memory_store, identity, sessions_root):
    clock = _fixed_clock()
    rec1 = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
        now=clock,
    )
    rec1.start()
    rec1.stop()
    assert not (sessions_root / "global_chain_root.json.bak").exists()
    rec2 = SessionRecorder(
        agent_identity=identity,
        memory_store=memory_store,
        sessions_root=sessions_root,
        enabled=True,
        now=clock,
    )
    rec2.start()
    rec2.stop()
    bak = sessions_root / "global_chain_root.json.bak"
    assert bak.exists()
    bak_data = json.loads(bak.read_text())
    assert bak_data["chain_length"] == 1
    assert bak_data["last_session_id"] == rec1.session_id


def test_chain_validates_across_multiple_sessions(memory_store, identity, sessions_root):
    clock = _fixed_clock()
    session_dirs = []
    for _ in range(3):
        rec = SessionRecorder(
            agent_identity=identity,
            memory_store=memory_store,
            sessions_root=sessions_root,
            enabled=True,
            now=clock,
        )
        session_dirs.append(rec.start())
        rec.stop()
    attestations = []
    for sd in session_dirs:
        attestations.extend(load_chain(sd / "attestation_chain.jsonl"))
    assert verify_chain(attestations, identity) is True


# ---------------------------------------------------------------------------
# orchestrator audio tap contract
# ---------------------------------------------------------------------------


def _make_stub_orchestrator():
    from src.orchestrator import Orchestrator

    class _NoopMemory:
        db_path = ":memory:"

        def count(self):
            return 0

        def recent_turns(self, n):
            return []

    class _StubCore:
        safety_layer = None
        style_reference = None
        mood_store = None

        def __init__(self):
            self.memory_store = _NoopMemory()

    orch = Orchestrator.__new__(Orchestrator)
    orch.persona_name = "renee"
    orch.state_dir = Path(".")
    orch._transcript_listeners = {}
    orch._legacy_emitter = None
    orch._audio_taps = {}
    orch.asr = None
    orch.tts = None
    orch._session_end_event = None
    return orch


def test_register_audio_tap_registry(memory_store, identity, sessions_root):
    orch = _make_stub_orchestrator()
    got = []
    unreg = orch.register_audio_tap(
        1,
        mic_cb=lambda b: got.append(("mic", b)),
        renee_cb=lambda b: got.append(("renee", b)),
    )
    assert orch.audio_tap_count() == 1
    unreg()
    assert orch.audio_tap_count() == 0


def test_feed_audio_tap_is_bit_for_bit():
    orch = _make_stub_orchestrator()
    captured = []
    orch.register_audio_tap(1, mic_cb=lambda b: captured.append(b))
    payloads = [
        bytes((i + k) % 256 for i in range(960 * 2))
        for k in range(5)
    ]

    async def _feed():
        for p in payloads:
            await orch.feed_audio(p)

    asyncio.run(_feed())
    assert captured == payloads


def test_tts_stream_tap_is_bit_for_bit_and_consumer_sees_same():
    """tts_output_stream yields to the consumer; the tap must observe the
    same bytes the consumer does."""
    orch = _make_stub_orchestrator()

    class _FakeTTS:
        async def stream(self):
            for k in range(4):
                yield bytes(((i + k * 3) % 256) for i in range(960 * 2))

    orch.tts = _FakeTTS()
    tap_chunks = []
    consumer_chunks = []
    orch.register_audio_tap(1, renee_cb=lambda b: tap_chunks.append(b))

    async def _consume():
        async for chunk in orch.tts_output_stream():
            consumer_chunks.append(chunk)

    asyncio.run(_consume())
    assert tap_chunks == consumer_chunks
    assert len(tap_chunks) == 4


def test_tap_failure_does_not_break_feed_audio():
    orch = _make_stub_orchestrator()

    def _boom(_):
        raise RuntimeError("tap failure should not propagate")

    orch.register_audio_tap(1, mic_cb=_boom)

    async def _feed():
        await orch.feed_audio(SAMPLE_PCM)

    asyncio.run(_feed())


# ---------------------------------------------------------------------------
# write_chain_root helper
# ---------------------------------------------------------------------------


def test_write_chain_root_atomic_and_bak(tmp_path):
    p = tmp_path / "global_chain_root.json"
    write_chain_root(p, {"chain_length": 1})
    assert p.exists()
    assert not p.with_suffix(".json.bak").exists()
    write_chain_root(p, {"chain_length": 2})
    bak = p.with_suffix(".json.bak")
    assert bak.exists()
    assert json.loads(bak.read_text())["chain_length"] == 1
    assert json.loads(p.read_text())["chain_length"] == 2
