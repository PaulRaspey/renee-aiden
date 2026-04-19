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
