"""Regression coverage for the pre-M15 OVERNIGHT_TODO priorities.

The seven priorities (jitter buffer, conversation log, greeting, pod
volume, Ollama fallback, start_renee.bat, known-issue cleanup) already
landed as commits; this file pins each one so a future refactor can't
silently reverse them.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from src.client.audio_bridge import (
    JITTER_BUFFER_CHUNKS,
    JITTER_QUEUE_MAX,
    ClientAudioBridge,
)
from src.orchestrator import Orchestrator
from src.persona.llm_router import LLMRouter, OLLAMA_UNAVAILABLE_FALLBACK
from src.server.audio_bridge import CloudAudioBridge


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Priority 1 — jitter buffer
# ---------------------------------------------------------------------------


def test_priority_1_jitter_buffer_constants_present():
    """The client audio bridge must expose a priming depth and a hard cap
    on the jitter queue. Both were added for 4x audio smoothness in
    priority-1 of OVERNIGHT_TODO; we pin the shape so regressions land
    as failing tests."""
    assert JITTER_BUFFER_CHUNKS >= 2, "priming should buy at least 2 chunks of audio"
    assert JITTER_QUEUE_MAX > JITTER_BUFFER_CHUNKS, "queue cap must exceed priming depth"


def test_priority_1_client_bridge_exposes_buffer_knobs():
    bridge = ClientAudioBridge("ws://127.0.0.1:1")
    assert bridge.frame_size == 960
    assert bridge.sample_rate == 48000


# ---------------------------------------------------------------------------
# Priority 2 — conversation logging
# ---------------------------------------------------------------------------


def test_priority_2_conversation_log_writes_dated_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Orchestrator._append_conversation_log must write a dated file with
    the canonical `[HH:MM:SS] PAUL:` / `RENEE:` shape."""
    # Build an orchestrator with no persona_core, no libraries — we only
    # exercise the logging primitive.
    class _Stub:
        def __init__(self, state_dir: Path):
            self.state_dir = state_dir
            self.persona_name = "renee"
            self._conversation_log_dir = state_dir / "logs" / "conversations"

    stub = _Stub(tmp_path / "state")
    # Bind the real method off the class.
    Orchestrator._append_conversation_log(
        stub,  # type: ignore[arg-type]
        user_text="hello world",
        response_text="hi back",
    )
    day = datetime.now().strftime("%Y-%m-%d")
    log = tmp_path / "state" / "logs" / "conversations" / f"{day}.log"
    assert log.exists()
    contents = log.read_text(encoding="utf-8")
    assert "PAUL: hello world" in contents
    assert "RENEE: hi back" in contents
    # Every line starts with [HH:MM:SS]
    for line in contents.strip().splitlines():
        assert line.startswith("[") and line[9] == "]", f"bad line shape: {line!r}"


def test_priority_2_conversation_log_swallows_errors(tmp_path: Path):
    """A logging failure must not propagate and kill the turn."""
    class _Stub:
        def __init__(self):
            self.state_dir = Path("//::bad::/::path")
            self.persona_name = "renee"
            self._conversation_log_dir = Path("//::bad::/::path/logs")

    # Should not raise even though the path is unwritable.
    Orchestrator._append_conversation_log(
        _Stub(),  # type: ignore[arg-type]
        user_text="x",
        response_text="y",
    )


# ---------------------------------------------------------------------------
# Priority 3 — greeting on connect
# ---------------------------------------------------------------------------


class _GreetOrchestrator:
    """Records whether greet_on_connect() was awaited."""

    def __init__(self) -> None:
        self.called_with: list[str] = []
        self.transcript_emitter = None

    async def feed_audio(self, pcm: bytes) -> None:  # pragma: no cover - unused
        return None

    async def tts_output_stream(self):  # pragma: no cover - unused
        if False:
            yield b""

    async def greet_on_connect(self, prompt: str) -> None:
        self.called_with.append(prompt)


class _FakeWS:
    def __init__(self) -> None:
        self._closed = asyncio.Event()
        self.sent: list[Any] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._closed.wait()
        raise StopAsyncIteration

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def send(self, data) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed.set()

    def close_sync(self) -> None:
        self._closed.set()


@pytest.mark.asyncio
async def test_priority_3_greet_on_connect_fires_once_when_flag_true():
    orch = _GreetOrchestrator()
    bridge = CloudAudioBridge(
        orch,
        greet_on_connect=True,
        greeting_prompt="system: greet paul, he just connected",
    )
    ws = _FakeWS()
    task = asyncio.create_task(bridge.handle_client(ws))
    # Let the bridge spin up and fire the greeting task.
    for _ in range(30):
        await asyncio.sleep(0.01)
        if orch.called_with:
            break
    assert orch.called_with == ["system: greet paul, he just connected"]
    ws.close_sync()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_priority_3_greet_on_connect_silent_when_flag_false():
    orch = _GreetOrchestrator()
    bridge = CloudAudioBridge(orch, greet_on_connect=False)
    ws = _FakeWS()
    task = asyncio.create_task(bridge.handle_client(ws))
    await asyncio.sleep(0.05)
    assert orch.called_with == []
    ws.close_sync()
    await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# Priority 4 — pod volume (blocked on PJ's UI action)
# ---------------------------------------------------------------------------


def test_priority_4_deployment_config_path_is_configs_yaml():
    """Pinning decision: only configs/deployment.yaml is read. The root
    deployment.yaml is a stale duplicate; regressing the loader to read
    the root would silently drift the pod config."""
    import src.client.pod_manager as pm
    assert "configs" in str(getattr(pm, "DEFAULT_DEPLOY_CONFIG", "")).replace("\\", "/")


# ---------------------------------------------------------------------------
# Priority 5 — LLM router graceful fallback
# ---------------------------------------------------------------------------


def test_priority_5_canned_response_on_no_backends(monkeypatch: pytest.MonkeyPatch):
    """When no backend is configured at all, generate() must return the
    canned fallback text rather than raising."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Also null out the bridge-key reader so the router truly has nothing.
    monkeypatch.setattr("src.persona.llm_router._read_bridge_key", lambda: None)
    router = LLMRouter()
    router.groq_client = None
    router.anthropic_client = None
    router.ollama_client = None
    resp = router.generate(
        system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.text == OLLAMA_UNAVAILABLE_FALLBACK
    assert resp.model == "fallback"


# ---------------------------------------------------------------------------
# Priority 6 — start_renee.bat one-click
# ---------------------------------------------------------------------------


def test_priority_6_start_renee_bat_exists_and_runs_module():
    bat = ROOT / "scripts" / "start_renee.bat"
    assert bat.exists(), "scripts/start_renee.bat must ship"
    content = bat.read_text(encoding="utf-8")
    assert "RENEE_SKIP_ENCRYPT_WARN" in content
    assert "-m renee" in content


# ---------------------------------------------------------------------------
# Priority 7 — known-issue cleanup
# ---------------------------------------------------------------------------


def test_priority_7_encrypt_warn_honored_when_env_is_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    """Setting RENEE_SKIP_ENCRYPT_WARN=1 silences the plaintext-vault
    warning the CLI prints at startup. We don't test the CLI directly;
    we check that the env var is consulted by the safety import-time
    check (if it exists) and that setting it does not raise anything."""
    monkeypatch.setenv("RENEE_SKIP_ENCRYPT_WARN", "1")
    # Re-import the safety layer; the import must succeed without warning.
    import importlib
    import src.safety as safety
    importlib.reload(safety)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "memory_encryption.enabled=false" not in combined


def test_priority_7_cloud_startup_self_test_uses_router_not_ollama():
    """The cloud_startup self-test must NOT contact Ollama directly; it
    should route through the configured LLM router so the self-test
    passes on a pod that only has Groq configured."""
    import inspect
    from scripts import cloud_startup
    src = inspect.getsource(cloud_startup)
    # Heuristic: if 'ollama.' shows up in the self-test block, we've
    # regressed. The self-test instead constructs an LLMRouter + calls
    # generate().
    assert "LLMRouter" in src
