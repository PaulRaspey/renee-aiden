"""Unit tests for src.voice.tts (M5 send path).

The ElevenLabs SDK is replaced with a fake client that returns canned
int16 PCM so the tests don't depend on the elevenlabs package or an
API key.
"""
from __future__ import annotations

import asyncio
from typing import List

import numpy as np
import pytest

from src.voice.tts import (
    BYTES_PER_SAMPLE,
    ELEVEN_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
    TTSConfig,
    TTSPipeline,
)


class FakeElClient:
    """Stand-in for scripts.el_client.ElClient. Returns a fixed tone
    at whatever sample rate the caller requested."""

    def __init__(self, duration_ms: int = 200):
        self.duration_ms = duration_ms
        self.calls: List[dict] = []

    def generate_pcm(self, params, max_retries: int = 6) -> bytes:
        self.calls.append(
            {
                "voice_id": params.voice_id,
                "text": params.text,
                "output_format": params.output_format,
                "sample_rate": params.sample_rate,
            }
        )
        n = int(params.sample_rate * self.duration_ms / 1000)
        t = np.arange(n) / params.sample_rate
        sig = (0.25 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
        return sig.tobytes()


@pytest.mark.asyncio
async def test_speak_enqueues_resampled_chunks_at_output_rate():
    pipe = TTSPipeline(TTSConfig(voice_id="voice-1"), client=FakeElClient(duration_ms=200))
    await pipe.speak("hello paul")

    # 200ms at 48000Hz = 9600 samples = 19200 bytes, framed into 20ms
    # chunks of 1920 bytes each → 10 chunks.
    expected_frame = (OUTPUT_SAMPLE_RATE * 20 // 1000) * BYTES_PER_SAMPLE
    assert expected_frame == 1920

    chunks: list[bytes] = []
    # Close first so stream() terminates; the queue already holds the frames.
    await pipe.close()
    async for chunk in pipe.stream():
        chunks.append(chunk)

    assert len(chunks) >= 9  # allow resample rounding
    assert all(len(c) == expected_frame for c in chunks[:-1])
    assert chunks[-1] and len(chunks[-1]) <= expected_frame


@pytest.mark.asyncio
async def test_speak_forwards_voice_and_format_to_client():
    fake = FakeElClient(duration_ms=60)
    pipe = TTSPipeline(TTSConfig(voice_id="voice-xyz"), client=fake)
    await pipe.speak("test")
    await pipe.close()
    # drain
    async for _ in pipe.stream():
        pass
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["voice_id"] == "voice-xyz"
    assert call["output_format"] == f"pcm_{ELEVEN_SAMPLE_RATE}"
    assert call["sample_rate"] == ELEVEN_SAMPLE_RATE
    assert call["text"] == "test"


@pytest.mark.asyncio
async def test_speak_ignores_empty_text_and_after_close():
    fake = FakeElClient()
    pipe = TTSPipeline(TTSConfig(voice_id="v"), client=fake)
    await pipe.speak("")
    await pipe.speak("   ")
    assert fake.calls == []
    await pipe.close()
    await pipe.speak("hello")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_close_unblocks_stream_waiter():
    pipe = TTSPipeline(TTSConfig(voice_id="v"), client=FakeElClient())

    consumed: list[bytes] = []

    async def consumer():
        async for c in pipe.stream():
            consumed.append(c)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)  # let it block on queue.get
    assert not task.done()
    await pipe.close()
    await asyncio.wait_for(task, timeout=1.0)
    assert consumed == []


@pytest.mark.asyncio
async def test_resample_shape_22050_to_48000():
    pcm_22k = (np.zeros(22050 // 10, dtype="<i2")).tobytes()   # 100ms
    out = TTSPipeline._resample_sync(pcm_22k, 22050, 48000)
    # 100ms at 48kHz ≈ 4800 samples = 9600 bytes; allow ±5% for filter edges.
    assert 9100 <= len(out) <= 10100


def test_resample_passthrough_when_rates_match():
    pcm = b"\x00" * 4800
    assert TTSPipeline._resample_sync(pcm, 48000, 48000) is pcm


@pytest.mark.asyncio
async def test_synthesis_failure_is_logged_and_swallowed():
    class BoomClient:
        def generate_pcm(self, params, max_retries: int = 6):
            raise RuntimeError("network down")

    pipe = TTSPipeline(TTSConfig(voice_id="v"), client=BoomClient())
    # speak() must not raise — failure is logged and nothing is enqueued.
    await pipe.speak("hi")
    await pipe.close()
    chunks: list[bytes] = []
    async for c in pipe.stream():
        chunks.append(c)
    assert chunks == []
