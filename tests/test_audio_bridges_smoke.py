"""Smoke tests for the audio bridge modules — they must import cleanly
without `websockets`, `opuslib`, or `sounddevice` installed."""
from __future__ import annotations

import pytest

from src.server.audio_bridge import CloudAudioBridge
from src.client.audio_bridge import ClientAudioBridge


class FakeOrchestrator:
    async def feed_audio(self, pcm: bytes) -> None:
        return None

    async def tts_output_stream(self):
        if False:
            yield b""


def test_cloud_bridge_constructs_without_codecs():
    # Construction must not touch opus.
    bridge = CloudAudioBridge(FakeOrchestrator())
    assert bridge.sample_rate == 48000
    assert bridge.frame_size == 960
    assert bridge._decoder is None
    assert bridge._encoder is None


def test_client_bridge_rejects_non_ws_url():
    with pytest.raises(ValueError):
        ClientAudioBridge("http://1.2.3.4:8765")


def test_client_bridge_accepts_ws_and_wss_urls():
    a = ClientAudioBridge("ws://1.2.3.4:8765")
    b = ClientAudioBridge("wss://secure.example.com:8765")
    assert a.server_url.startswith("ws")
    assert b.server_url.startswith("wss")
