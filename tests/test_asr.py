"""Unit tests for src.voice.asr (M1).

The faster-whisper model is replaced with a fake so the tests don't need
the package installed. Audio frames are synthesized from numpy directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pytest

from src.voice.asr import ASRConfig, ASRPipeline, _resample_to_16k


# ----- fakes -----------------------------------------------------------------


@dataclass
class FakeSegment:
    text: str


class FakeWhisper:
    """Stub that returns a canned transcript on each transcribe()."""

    def __init__(self, transcripts: Optional[list[str]] = None):
        self.transcripts = list(transcripts or [])
        self.calls: list[int] = []  # sample lengths
        self._default = "hello world"

    def transcribe(self, audio: np.ndarray, beam_size: int = 1):
        self.calls.append(len(audio))
        text = self.transcripts.pop(0) if self.transcripts else self._default
        return [FakeSegment(text=text)], None


# ----- frame generation ------------------------------------------------------


FRAME_SIZE = 960  # 20ms at 48kHz


def _speech_frame(amplitude: float = 0.3) -> bytes:
    # 400 Hz tone at 48kHz: loud enough to clear the rms threshold.
    t = np.arange(FRAME_SIZE) / 48000.0
    sig = (amplitude * np.sin(2 * np.pi * 400 * t) * 32767).astype(np.int16)
    return sig.tobytes()


def _silent_frame() -> bytes:
    return (np.zeros(FRAME_SIZE, dtype=np.int16)).tobytes()


# ----- helpers ---------------------------------------------------------------


async def _drain_tasks(pipeline: ASRPipeline) -> None:
    # Wait for any background transcription tasks to finish.
    while pipeline._tasks:
        await asyncio.wait(list(pipeline._tasks))


# ----- tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_silence_only_does_not_trigger_callbacks():
    fake = FakeWhisper()
    cfg = ASRConfig(partial_interval_ms=100, silence_finalize_ms=200, min_speech_ms=100)
    partial_calls: list = []
    final_calls: list = []
    pipe = ASRPipeline(
        cfg,
        on_partial=lambda t, s: partial_calls.append((t, s)) or asyncio.sleep(0),
        on_final=lambda t: final_calls.append(t) or asyncio.sleep(0),
        whisper_model=fake,
    )
    for _ in range(20):  # 400ms of silence
        await pipe.feed_audio(_silent_frame())
    await _drain_tasks(pipe)
    assert partial_calls == []
    assert final_calls == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_speech_then_silence_emits_final():
    fake = FakeWhisper(transcripts=["hey renee", "hey renee"])
    cfg = ASRConfig(
        partial_interval_ms=60,
        silence_finalize_ms=120,
        min_speech_ms=60,
        silence_rms_threshold=0.01,
    )
    finals: list[str] = []
    partials: list[tuple[str, int]] = []

    async def on_partial(text: str, silence_ms: int) -> None:
        partials.append((text, silence_ms))

    async def on_final(text: str) -> None:
        finals.append(text)

    pipe = ASRPipeline(cfg, on_partial=on_partial, on_final=on_final, whisper_model=fake)

    # 300ms of speech
    for _ in range(15):
        await pipe.feed_audio(_speech_frame())
        await asyncio.sleep(0)  # let background tasks run
    # 300ms of silence — crosses finalize threshold
    for _ in range(15):
        await pipe.feed_audio(_silent_frame())
        await asyncio.sleep(0)

    await _drain_tasks(pipe)

    assert finals == ["hey renee"]
    # at least one partial fired before the final
    assert len(partials) >= 1
    assert partials[0][0] == "hey renee"


@pytest.mark.asyncio
async def test_no_overlapping_transcriptions():
    """While a partial is in flight, feed_audio must not kick off another."""
    gate = asyncio.Event()

    class SlowWhisper(FakeWhisper):
        def __init__(self):
            super().__init__()
            self.started = 0

        def transcribe(self, audio: np.ndarray, beam_size: int = 1):
            self.started += 1
            # Block until the gate is released. Runs in a thread so awaiting
            # the gate from here would be wrong — instead spin on the flag.
            while not gate.is_set():
                pass
            return [FakeSegment(text="x")], None

    fake = SlowWhisper()
    cfg = ASRConfig(partial_interval_ms=20, min_speech_ms=20, silence_finalize_ms=9_999)
    pipe = ASRPipeline(cfg, whisper_model=fake)

    # Pump a bunch of speech frames so the partial fires and stalls.
    for _ in range(20):
        await pipe.feed_audio(_speech_frame())
        await asyncio.sleep(0)

    # Pump more frames while the transcription is blocked.
    for _ in range(20):
        await pipe.feed_audio(_speech_frame())
        await asyncio.sleep(0)

    # Exactly one transcribe() should have started despite dozens of ticks.
    assert fake.started == 1
    gate.set()
    await _drain_tasks(pipe)


@pytest.mark.asyncio
async def test_close_cancels_pending_tasks():
    fake = FakeWhisper()
    pipe = ASRPipeline(
        ASRConfig(partial_interval_ms=20, min_speech_ms=20),
        whisper_model=fake,
    )
    for _ in range(15):
        await pipe.feed_audio(_speech_frame())
    await pipe.close()
    # After close, further frames are ignored.
    await pipe.feed_audio(_speech_frame())
    assert pipe._closed is True


def test_resample_48k_to_16k_shape():
    src = np.ones(4800, dtype=np.float32)
    out = _resample_to_16k(src, 48000)
    # 48kHz -> 16kHz is 3:1, so ~1600 samples.
    assert 1500 <= out.size <= 1700
    assert out.dtype == np.float32


def test_resample_passthrough_when_already_16k():
    src = np.zeros(1600, dtype=np.float32)
    out = _resample_to_16k(src, 16000)
    assert out is src
