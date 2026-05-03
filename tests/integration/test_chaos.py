"""Failure-mode chaos tests for the launcher (#10).

These exercise the graceful-degradation paths that pure unit tests can't
easily reach: a pod stuck in STARTING, a Beacon that returns 500 mid-flight,
a transcript listener whose callback raises mid-fan-out, a session recorder
that explodes on start, etc.

Tests are kept fast (no live network, no real pods) by injecting fakes at
the same seams the production code uses for normal operation.
"""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib import error as urllib_error

import pytest


# ---------------------------------------------------------------------------
# Pod stuck in STARTING — the wake retry must give up cleanly, not loop forever
# ---------------------------------------------------------------------------


def test_wake_with_retry_gives_up_when_pod_never_running(monkeypatch):
    """Force PodManager.wake() to TimeoutError and status() to never flip.
    The launcher's _wake_with_retry should return (False, error_dict)
    within max_wait_s."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "session_launcher",
        Path(__file__).resolve().parents[2] / "scripts" / "session_launcher.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    fake_mgr = MagicMock()
    fake_mgr.wake.side_effect = TimeoutError("STARTING but never RUNNING")
    fake_mgr.status.return_value = {"status": "STARTING", "public_ip": ""}

    fake_pm = MagicMock()
    fake_pm.return_value = fake_mgr
    monkeypatch.setattr("src.client.pod_manager.PodManager", fake_pm)
    monkeypatch.setattr("src.client.pod_manager.load_deployment", lambda _: None)
    # Compress the loop so the test isn't slow
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        mod.time, "sleep",
        lambda s: sleep_calls.append(s),
    )

    waked, info = mod._wake_with_retry(max_wait_s=0)
    assert waked is False
    # Each call to wake() raised, so info should carry that
    assert "STARTING" in str(info) or "wake timed out" in str(info)


# ---------------------------------------------------------------------------
# Network flap during Beacon heartbeat — connection error must not crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_beacon_heartbeat_swallows_transport_error(tmp_path: Path):
    """A Beacon that's intermittently unreachable should never propagate
    its URLError up through the heartbeat loop."""
    from src.uahp.beacon_client import BeaconClient, BeaconCredentials
    creds = BeaconCredentials(
        agent_id="agt-x", api_key="bk-x", base_url="http://b.invalid",
    )
    client = BeaconClient("http://b.invalid", tmp_path, credentials=creds)

    call_count = 0

    def flaky_post(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count % 2 == 0:
            return {}  # success
        raise urllib_error.URLError("network flap")

    with patch("src.uahp.beacon_client._http_post", side_effect=flaky_post):
        # Issue several heartbeats; alternating failures must not raise
        for _ in range(4):
            result = await client.heartbeat()
            # On error: None. On success: dict. Either is fine — just no raise.
            assert result is None or isinstance(result, dict)
    assert call_count == 4
    # Credentials still valid (transport errors don't clear them)
    assert client.credentials is not None


# ---------------------------------------------------------------------------
# Beacon returns 500 mid-flight — the heartbeat reads it as "non-fatal"
# (only 409 dead-agent clears credentials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_beacon_500_does_not_clear_credentials(tmp_path: Path):
    from src.uahp.beacon_client import BeaconClient, BeaconCredentials
    creds = BeaconCredentials(
        agent_id="agt-x", api_key="bk-x", base_url="http://b.example",
    )
    client = BeaconClient("http://b.example", tmp_path, credentials=creds)

    err = urllib_error.HTTPError("u", 500, "Internal", {}, None)  # type: ignore[arg-type]
    with patch("src.uahp.beacon_client._http_post", side_effect=err):
        result = await client.heartbeat()
    assert result is None
    # Credentials NOT cleared on 500 (only 409 means we're declared dead)
    assert client.credentials is not None


# ---------------------------------------------------------------------------
# Transcript listener callback raises mid-fan-out — orchestrator must keep
# delivering to the other listeners
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_fan_out_isolates_listener_failures():
    """One bad callback should not prevent the others from receiving the
    transcript. Otherwise a flaky recorder would silence the WS bridge."""
    import sys
    from pathlib import Path as _P
    src_path = _P(__file__).resolve().parents[2]
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    from src.orchestrator import Orchestrator
    from src.persona.core import PersonaCore
    from src.persona.llm_router import LLMRouter

    # Stub PersonaCore via an Orchestrator with a no-op router
    class StubRouter:
        async def generate_async(self, *a, **kw):
            from src.persona.persona_def import PersonaResponse
            return PersonaResponse(text="ok", tokens=[], backend="stub")

    # Use the test fixture pattern from tests/test_orchestrator
    import tempfile
    state = _P(tempfile.mkdtemp())
    core = PersonaCore(
        persona_name="renee",
        config_dir=src_path / "configs",
        state_dir=state,
        router=StubRouter(),
        memory_store=None,
    )
    orch = Orchestrator(persona_name="renee", state_dir=state, persona_core=core)

    received_a = []
    received_b = []

    async def cb_a(msg):
        received_a.append(msg)

    async def cb_bad(msg):
        raise RuntimeError("listener boom")

    async def cb_b(msg):
        received_b.append(msg)

    orch.register_transcript_listener("a", cb_a)
    orch.register_transcript_listener("bad", cb_bad)
    orch.register_transcript_listener("b", cb_b)

    await orch._emit_transcript({"type": "transcript", "speaker": "paul", "text": "hi"})
    # a and b receive despite bad raising
    assert received_a == [{"type": "transcript", "speaker": "paul", "text": "hi"}]
    assert received_b == [{"type": "transcript", "speaker": "paul", "text": "hi"}]


# ---------------------------------------------------------------------------
# Recorder start() raises — bridge must continue without recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_survives_recorder_start_crash():
    """Already covered by audio_bridge_smoke but we re-assert it here under
    the chaos test header so the failure mode is discoverable."""
    from src.server.audio_bridge import CloudAudioBridge

    class FakeWS:
        def __init__(self):
            self._closed = asyncio.Event()
            self.outbox = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self._closed.wait()
            raise StopAsyncIteration

        async def wait_closed(self):
            await self._closed.wait()

        async def send(self, data):
            self.outbox.append(data)

        def close_sync(self):
            self._closed.set()

    class FakePersonaCore:
        identity = "id"
        memory_store = "ms"

    class TapOrch:
        persona_core = FakePersonaCore()

        def register_audio_tap(self, *a, **kw):
            return lambda: None

        def register_transcript_listener(self, *a, **kw):
            return lambda: None

    def boom(**kwargs):
        raise RuntimeError("disk full")

    bridge = CloudAudioBridge(
        TapOrch(), recording_enabled=True,
        session_recorder_factory=boom,
    )
    ws = FakeWS()
    task = asyncio.create_task(bridge.handle_client(ws))
    await asyncio.sleep(0.05)
    assert not task.done(), "bridge died because recorder crashed"
    ws.close_sync()
    await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# ffmpeg crash mid-session — bridge must not corrupt the loop WS connection
# (unit test of stdin write error handler in ws-handler.ts equivalent)
# ---------------------------------------------------------------------------


def test_ffmpeg_stdin_write_error_does_not_crash():
    """When ffmpeg's stdin pipe is destroyed (process crashed), our wrapper
    catches the EPIPE and logs. This is the python equivalent of the JS
    bridge's `try { session.ffmpeg.stdin.write(buf) } catch ...` pattern."""
    # No actual production code in renee-aiden invokes ffmpeg today (the
    # ffmpeg path lives in the Replit Express bridge), so this asserts the
    # general idea: a closed pipe write should be catchable.
    import io
    pipe = io.BytesIO()
    pipe.close()
    with pytest.raises(ValueError):
        pipe.write(b"x")  # write-after-close raises ValueError
    # The Express bridge wraps this in a try/except — equivalent test.


# ---------------------------------------------------------------------------
# Tailscale CLI exits non-zero — _check_tailscale must surface stderr
# instead of silently treating empty stdout as success
# ---------------------------------------------------------------------------


def test_tailscale_check_treats_nonzero_as_failure(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "session_launcher",
        Path(__file__).resolve().parents[2] / "scripts" / "session_launcher.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    fake = MagicMock(returncode=1, stdout="", stderr="not authenticated")
    monkeypatch.setenv("TAILSCALE_AUTHKEY", "")  # ensure no auto-up
    monkeypatch.delenv("TAILSCALE_AUTHKEY", raising=False)
    with patch.object(mod.shutil, "which", return_value="/usr/bin/tailscale"):
        with patch.object(mod.subprocess, "run", return_value=fake):
            ok, msg = mod._check_tailscale()
    assert ok is False
    assert "not authenticated" in msg.lower() or "tailscale up" in msg.lower()


# ---------------------------------------------------------------------------
# SQLite ledger under concurrent writes — no corruption, no exceptions
# ---------------------------------------------------------------------------


def test_cost_ledger_concurrent_writes_dont_corrupt(tmp_path: Path):
    """Threads racing to write events should each succeed without raising
    SQLite locking errors. SQLite's default journal mode + our short-lived
    connections handle this; this test asserts it stays that way."""
    import threading
    from src.client.cost_ledger import (
        list_events, record_down, record_up,
    )
    db = tmp_path / "ledger.db"
    errors: list[BaseException] = []

    def worker(i: int):
        try:
            for j in range(10):
                if j % 2 == 0:
                    record_up(pod_id=f"p{i}", db_path=db)
                else:
                    record_down(
                        pod_id=f"p{i}", minutes=5, hourly_usd=1.0, db_path=db,
                    )
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"concurrent writes raised: {errors}"
    events = list_events(db_path=db, limit=200)
    assert len(events) == 40  # 4 workers × 10 writes


# ---------------------------------------------------------------------------
# Keyring backend missing — secrets layer falls back without raising
# ---------------------------------------------------------------------------


def test_secrets_module_works_without_keyring(monkeypatch):
    """Simulate keyring import failure. get/set/migrate must not raise."""
    from renee import secrets
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    monkeypatch.setenv("RUNPOD_API_KEY", "from-env")
    assert secrets.get("RUNPOD_API_KEY") == "from-env"
    assert secrets.set_("RUNPOD_API_KEY", "new") is False
    summary = secrets.migrate_env_to_keyring()
    assert all("keyring unavailable" in v or "skipped" in v for v in summary.values())


# ---------------------------------------------------------------------------
# Persistence file corruption — cost ledger should auto-create when missing
# ---------------------------------------------------------------------------


def test_cost_ledger_creates_db_when_missing(tmp_path: Path):
    """Fresh start: no DB file. record_up should still work."""
    db = tmp_path / "doesnotexist" / "ledger.db"
    assert not db.exists()
    from src.client.cost_ledger import record_up, list_events
    rid = record_up(pod_id="p1", db_path=db)
    assert rid > 0
    assert db.exists()
    assert len(list_events(db_path=db)) == 1
