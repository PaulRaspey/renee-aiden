"""
Streaming ASR pipeline (M1).

The cloud audio bridge (M14) hands us 20ms frames of 48kHz mono int16 PCM.
This module buffers those frames and runs faster-whisper against a rolling
window, emitting:

  - `on_partial(text, silence_ms)` roughly every `partial_interval_ms` while
    the user is speaking (for the turn-taking endpointer + backchannel).
  - `on_final(text)` once trailing silence crosses `silence_finalize_ms`
    (for the persona core turn).

Transcription runs in `asyncio.to_thread` so the event loop keeps pulling
frames off the websocket while whisper is busy. An in-flight flag prevents
overlapping transcriptions — if a partial is still running when the next
interval fires, we skip that tick rather than queue up work.

faster-whisper is lazy-imported: this module imports cleanly on a host
that hasn't `pip install faster-whisper`'d yet. On the RunPod pod:
    pip install faster-whisper>=1.0.0

Tests inject a fake model via `whisper_model=...` to avoid the dep.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from math import gcd
from typing import Any, Awaitable, Callable, Optional

import numpy as np


logger = logging.getLogger("renee.voice.asr")


INPUT_SAMPLE_RATE = 48000
WHISPER_SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # int16


PartialCallback = Callable[[str, int], Awaitable[None]]
FinalCallback = Callable[[str], Awaitable[None]]


@dataclass
class ASRConfig:
    model: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"
    input_sample_rate: int = INPUT_SAMPLE_RATE
    partial_interval_ms: int = 400
    silence_finalize_ms: int = 700
    silence_rms_threshold: float = 0.01
    min_speech_ms: int = 200
    max_buffer_seconds: float = 30.0
    beam_size: int = 1


def _load_whisper(cfg: ASRConfig):
    from faster_whisper import WhisperModel
    return WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)


class ASRPipeline:
    def __init__(
        self,
        config: Optional[ASRConfig] = None,
        *,
        on_partial: Optional[PartialCallback] = None,
        on_final: Optional[FinalCallback] = None,
        whisper_model: Any = None,
    ):
        self.config = config or ASRConfig()
        self.on_partial = on_partial
        self.on_final = on_final
        self._model = whisper_model

        self._buffer = bytearray()
        self._silence_ms = 0
        self._speech_ms = 0
        self._last_partial_ts = 0.0
        self._last_partial_text = ""
        self._lock = asyncio.Lock()
        self._finalize_scheduled = False
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    # ---------------- public API ----------------

    async def feed_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return

        duration_ms = int(1000 * samples.size / self.config.input_sample_rate)
        rms = _rms(samples)
        is_silent = rms < self.config.silence_rms_threshold

        if is_silent:
            self._silence_ms += duration_ms
        else:
            self._silence_ms = 0
            self._speech_ms += duration_ms

        self._buffer.extend(pcm)
        self._trim_buffer()

        # endpoint — schedule finalize even if a partial is currently
        # running; _finalize will block on the lock until the partial
        # completes, then transcribe the full buffer.
        if (
            self._silence_ms >= self.config.silence_finalize_ms
            and self._speech_ms >= self.config.min_speech_ms
            and not self._finalize_scheduled
        ):
            self._finalize_scheduled = True
            self._spawn(self._finalize())
            return

        if self._finalize_scheduled:
            return

        # partial tick — skip if a transcription is already in flight;
        # frames keep accumulating in the buffer for the next one.
        now = time.monotonic()
        since_last = (now - self._last_partial_ts) * 1000.0
        if (
            since_last >= self.config.partial_interval_ms
            and self._speech_ms >= self.config.min_speech_ms
            and not self._lock.locked()
        ):
            self._last_partial_ts = now
            self._spawn(self._run_partial())

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # ---------------- internals ----------------

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _trim_buffer(self) -> None:
        max_bytes = int(self.config.max_buffer_seconds * self.config.input_sample_rate) * BYTES_PER_SAMPLE
        if len(self._buffer) > max_bytes:
            del self._buffer[: len(self._buffer) - max_bytes]

    async def _run_partial(self) -> None:
        async with self._lock:
            if self._finalize_scheduled:
                # A finalize was queued while we were waiting; let it
                # handle the buffer instead of emitting a stale partial.
                return
            pcm = bytes(self._buffer)
            text = await asyncio.to_thread(self._transcribe_sync, pcm)
            if text and text != self._last_partial_text and self.on_partial is not None:
                self._last_partial_text = text
                try:
                    await self.on_partial(text, self._silence_ms)
                except Exception:
                    logger.exception("on_partial raised")

    async def _finalize(self) -> None:
        async with self._lock:
            try:
                pcm = bytes(self._buffer)
                self._buffer.clear()
                self._silence_ms = 0
                self._speech_ms = 0
                self._last_partial_text = ""
                self._last_partial_ts = 0.0
                if not pcm:
                    return
                text = await asyncio.to_thread(self._transcribe_sync, pcm)
                if text and self.on_final is not None:
                    try:
                        await self.on_final(text)
                    except Exception:
                        logger.exception("on_final raised")
            finally:
                self._finalize_scheduled = False

    def _transcribe_sync(self, pcm_bytes: bytes) -> str:
        if not pcm_bytes:
            return ""
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        audio16k = _resample_to_16k(samples, self.config.input_sample_rate)
        if audio16k.size == 0:
            return ""
        model = self._ensure_model()
        segments, _info = model.transcribe(audio16k, beam_size=self.config.beam_size)
        return " ".join(seg.text.strip() for seg in segments).strip()

    def _ensure_model(self):
        if self._model is None:
            self._model = _load_whisper(self.config)
        return self._model


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    x = samples.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def _resample_to_16k(audio: np.ndarray, src_sr: int) -> np.ndarray:
    if src_sr == WHISPER_SAMPLE_RATE:
        return audio
    from scipy.signal import resample_poly
    g = gcd(src_sr, WHISPER_SAMPLE_RATE)
    up = WHISPER_SAMPLE_RATE // g
    down = src_sr // g
    return resample_poly(audio, up, down).astype(np.float32)
