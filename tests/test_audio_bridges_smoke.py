"""Smoke tests for the audio bridge modules — they must import cleanly
without `websockets` or `sounddevice` installed."""
from __future__ import annotations

import asyncio

import pytest

from src.server.audio_bridge import CloudAudioBridge
from src.client.audio_bridge import ClientAudioBridge


class FakeOrchestrator:
    async def feed_audio(self, pcm: bytes) -> None:
        return None

    async def tts_output_stream(self):
        if False:
            yield b""


class BareOrchestrator:
    """No feed_audio, no tts_output_stream — the state the bridge used to
    crash on. After the M1 fix, it should drain + park instead."""


class FakeWebSocket:
    """Minimal async-iterable websocket: yields queued messages, then
    blocks until close() is called (ws.wait_closed awaits that same event).
    """

    def __init__(self, messages: list[bytes]):
        self._messages = list(messages)
        self._closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        await self._closed.wait()
        raise StopAsyncIteration

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def send(self, data) -> None:
        pass

    def close(self) -> None:
        self._closed.set()


def test_cloud_bridge_constructs_cleanly():
    bridge = CloudAudioBridge(FakeOrchestrator())
    assert bridge.sample_rate == 48000
    assert bridge.frame_size == 960


def test_client_bridge_rejects_non_ws_url():
    with pytest.raises(ValueError):
        ClientAudioBridge("http://1.2.3.4:8765")


def test_client_bridge_accepts_ws_and_wss_urls():
    a = ClientAudioBridge("ws://1.2.3.4:8765")
    b = ClientAudioBridge("wss://secure.example.com:8765")
    assert a.server_url.startswith("ws")
    assert b.server_url.startswith("wss")


@pytest.mark.asyncio
async def test_receive_audio_drains_when_feed_audio_absent(caplog):
    """Bridge must consume frames (not return) when orchestrator lacks feed_audio."""
    bridge = CloudAudioBridge(BareOrchestrator())
    ws = FakeWebSocket([b"\x00" * 1920, b"\x00" * 1920, b"\x00" * 1920])
    task = asyncio.create_task(bridge._receive_audio(ws))
    await asyncio.sleep(0.05)  # let it drain the queue
    assert not task.done(), "receive must not exit while ws is open"
    ws.close()
    await task  # should return cleanly once ws closes


@pytest.mark.asyncio
async def test_send_audio_waits_for_close_when_tts_stream_absent():
    """Without tts_output_stream the send task parks on wait_closed."""
    bridge = CloudAudioBridge(BareOrchestrator())
    ws = FakeWebSocket([])
    task = asyncio.create_task(bridge._send_audio(ws))
    await asyncio.sleep(0.02)
    assert not task.done(), "send must wait for ws close, not return"
    ws.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_handle_client_stays_open_with_bare_orchestrator():
    """Regression: with a BareOrchestrator, the connection should not close
    immediately just because both orchestrator methods are missing."""
    bridge = CloudAudioBridge(BareOrchestrator())
    ws = FakeWebSocket([b"\x00" * 1920])
    task = asyncio.create_task(bridge.handle_client(ws))
    await asyncio.sleep(0.05)
    assert not task.done(), "handler closed the connection prematurely"
    ws.close()
    await asyncio.wait_for(task, timeout=1.0)


class EmitterOrchestrator:
    """Minimal orchestrator that exposes a transcript_emitter slot.

    Deliberately omits tts_output_stream so _send_audio parks on
    ws.wait_closed() instead of exiting immediately on an empty stream.
    """

    def __init__(self):
        self.transcript_emitter = None

    async def feed_audio(self, pcm: bytes) -> None:
        return None


@pytest.mark.asyncio
async def test_handle_client_installs_transcript_emitter_for_connection():
    """The bridge must install an emitter on connect and clear it on disconnect."""
    import json

    orch = EmitterOrchestrator()
    bridge = CloudAudioBridge(orch)
    ws = FakeWebSocket([])
    task = asyncio.create_task(bridge.handle_client(ws))
    # Let handle_client install the emitter before we test it.
    await asyncio.sleep(0.02)
    assert callable(orch.transcript_emitter), "emitter not installed"

    # Invoking the emitter sends a JSON text frame over the websocket.
    sent: list = []
    orig_send = ws.send

    async def capture(data):
        sent.append(data)
        await orig_send(data)

    ws.send = capture  # type: ignore[assignment]
    await orch.transcript_emitter(
        {"type": "transcript", "speaker": "paul", "text": "hello"}
    )
    assert sent and sent[0] == json.dumps(
        {"type": "transcript", "speaker": "paul", "text": "hello"}
    )

    ws.close()
    await asyncio.wait_for(task, timeout=1.0)
    assert orch.transcript_emitter is None, "emitter not cleared on disconnect"


# ---------------------------------------------------------------------------
# Per-connection recorder wiring (#1 — closes Part 2 deferred)
# ---------------------------------------------------------------------------


class FakePersonaCore:
    def __init__(self):
        self.identity = "fake-identity"
        self.memory_store = "fake-memory-store"


class TapOrchestrator:
    """Orchestrator surface the bridge needs for per-connection recording:
    persona_core (for identity + memory_store), register_audio_tap, and
    register_transcript_listener."""

    def __init__(self):
        self.persona_core = FakePersonaCore()
        self.audio_taps: dict = {}
        self.transcript_listeners: dict = {}

    def register_audio_tap(self, conn_id, mic_cb=None, renee_cb=None):
        self.audio_taps[conn_id] = (mic_cb, renee_cb)

        def _remove():
            self.audio_taps.pop(conn_id, None)
        return _remove

    def register_transcript_listener(self, conn_id, cb):
        self.transcript_listeners[conn_id] = cb

        def _remove():
            self.transcript_listeners.pop(conn_id, None)
        return _remove


class FakeRecorder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    def on_mic_pcm(self, pcm: bytes) -> None: pass
    def on_renee_pcm(self, pcm: bytes) -> None: pass
    async def on_transcript_async(self, msg: dict) -> None: pass

    def start(self):
        self.started = True
        return "fake/session/dir"

    def stop(self):
        self.stopped = True
        return None


@pytest.mark.asyncio
async def test_recorder_taps_audio_when_recording_enabled():
    orch = TapOrchestrator()
    rec_holder: list = []

    def factory(**kwargs):
        rec = FakeRecorder(**kwargs)
        rec_holder.append(rec)
        return rec

    bridge = CloudAudioBridge(
        orch, recording_enabled=True, session_recorder_factory=factory,
    )
    ws = FakeWebSocket([])
    task = asyncio.create_task(bridge.handle_client(ws))
    await asyncio.sleep(0.05)
    # Recorder constructed + started + audio_tap registered
    assert len(rec_holder) == 1
    rec = rec_holder[0]
    assert rec.started is True
    assert rec.kwargs["agent_identity"] == "fake-identity"
    assert rec.kwargs["memory_store"] == "fake-memory-store"
    # Audio tap is keyed `recorder:<id(ws)>`
    tap_keys = list(orch.audio_taps.keys())
    assert any(str(k).startswith("recorder:") for k in tap_keys)
    # Transcript listener for the recorder is also registered (separate key)
    listener_keys = list(orch.transcript_listeners.keys())
    assert any(str(k).startswith("recorder-tr:") for k in listener_keys)
    ws.close()
    await asyncio.wait_for(task, timeout=1.0)
    # On disconnect, recorder.stop() ran AND tap/listener unregistered
    assert rec.stopped is True
    assert all(not str(k).startswith("recorder") for k in orch.audio_taps.keys())
    assert all(not str(k).startswith("recorder-tr") for k in orch.transcript_listeners.keys())


@pytest.mark.asyncio
async def test_no_recorder_when_recording_disabled():
    orch = TapOrchestrator()
    rec_holder: list = []

    def factory(**kwargs):
        rec = FakeRecorder(**kwargs)
        rec_holder.append(rec)
        return rec

    bridge = CloudAudioBridge(
        orch, recording_enabled=False, session_recorder_factory=factory,
    )
    ws = FakeWebSocket([])
    task = asyncio.create_task(bridge.handle_client(ws))
    await asyncio.sleep(0.05)
    assert len(rec_holder) == 0  # factory never called
    assert not any(str(k).startswith("recorder:") for k in orch.audio_taps.keys())
    ws.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_no_recorder_when_persona_core_missing():
    """Bare orchestrator without persona_core: recording silently skips."""
    bridge = CloudAudioBridge(
        BareOrchestrator(), recording_enabled=True,
        session_recorder_factory=lambda **kw: FakeRecorder(**kw),
    )
    ws = FakeWebSocket([])
    task = asyncio.create_task(bridge.handle_client(ws))
    await asyncio.sleep(0.05)
    # Bridge survives — no crash, just no recorder
    assert not task.done()
    ws.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_recorder_failure_does_not_crash_bridge():
    """If start() raises, the bridge logs and continues without a recorder."""
    orch = TapOrchestrator()

    def boom(**kwargs):
        rec = FakeRecorder(**kwargs)
        rec.start = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))  # type: ignore[assignment]
        return rec

    bridge = CloudAudioBridge(
        orch, recording_enabled=True, session_recorder_factory=boom,
    )
    ws = FakeWebSocket([])
    task = asyncio.create_task(bridge.handle_client(ws))
    await asyncio.sleep(0.05)
    # No tap/listener registered (recorder failed to start)
    assert not any(str(k).startswith("recorder:") for k in orch.audio_taps.keys())
    # Bridge still alive
    assert not task.done()
    ws.close()
    await asyncio.wait_for(task, timeout=1.0)


def test_recording_should_run_reads_env(monkeypatch):
    bridge = CloudAudioBridge(BareOrchestrator())
    monkeypatch.setenv("RENEE_RECORD", "1")
    assert bridge._recording_should_run() is True
    monkeypatch.setenv("RENEE_RECORD", "0")
    assert bridge._recording_should_run() is False
    monkeypatch.delenv("RENEE_RECORD", raising=False)
    assert bridge._recording_should_run() is False


def test_recording_should_run_override_wins(monkeypatch):
    monkeypatch.setenv("RENEE_RECORD", "0")
    bridge = CloudAudioBridge(BareOrchestrator(), recording_enabled=True)
    assert bridge._recording_should_run() is True
    bridge2 = CloudAudioBridge(BareOrchestrator(), recording_enabled=False)
    monkeypatch.setenv("RENEE_RECORD", "1")
    assert bridge2._recording_should_run() is False


# ---------------------------------------------------------------------------
# set_topic JSON dispatch (#2)
# ---------------------------------------------------------------------------


class TopicOrchestrator:
    def __init__(self):
        self.topic_calls: list = []

    def set_session_topic(self, topic):
        self.topic_calls.append(topic)


def test_dispatch_text_set_topic_calls_orchestrator():
    orch = TopicOrchestrator()
    bridge = CloudAudioBridge(orch)
    bridge._dispatch_text_message('{"type": "set_topic", "text": "memory consolidation"}')
    assert orch.topic_calls == ["memory consolidation"]


def test_dispatch_text_set_topic_accepts_topic_field_alias():
    """Either 'text' or 'topic' is accepted to be lenient with PWA versions."""
    orch = TopicOrchestrator()
    bridge = CloudAudioBridge(orch)
    bridge._dispatch_text_message('{"type": "set_topic", "topic": "via alias"}')
    assert orch.topic_calls == ["via alias"]


def test_dispatch_text_unknown_type_is_silent():
    orch = TopicOrchestrator()
    bridge = CloudAudioBridge(orch)
    bridge._dispatch_text_message('{"type": "future_feature", "data": 42}')
    assert orch.topic_calls == []  # nothing dispatched


def test_dispatch_text_invalid_json_does_not_crash():
    orch = TopicOrchestrator()
    bridge = CloudAudioBridge(orch)
    bridge._dispatch_text_message("not even json")
    bridge._dispatch_text_message("")
    bridge._dispatch_text_message("[1,2,3]")  # not a dict
    assert orch.topic_calls == []


def test_dispatch_text_handles_orchestrator_without_set_session_topic():
    """Bare orchestrator: topic is dropped silently."""
    bridge = CloudAudioBridge(BareOrchestrator())
    # Should not raise
    bridge._dispatch_text_message('{"type": "set_topic", "text": "x"}')
