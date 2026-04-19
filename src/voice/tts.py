"""
ElevenLabs TTS pipeline (M5 send path).

Sits on the bridge's send side. `speak(text)` renders the persona's
reply through ElevenLabs via scripts.el_client.ElClient, resamples from
ElevenLabs' 22050Hz PCM to the wire/client rate (48000Hz), chunks into
20ms frames, and pushes them onto an asyncio.Queue. `stream()` is the
async generator the bridge's _send_audio iterates.

The queue is the decoupling point — the orchestrator never touches the
websocket directly; the bridge pulls chunks as fast as the network will
accept them. `close()` drains the stream via a `None` sentinel.

The elevenlabs SDK is heavy and lazy-imported through scripts.el_client.
Tests inject a fake via `client=...` to avoid the network round-trip.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from math import gcd
from typing import Any, AsyncIterator, Optional

import numpy as np


logger = logging.getLogger("renee.voice.tts")


OUTPUT_SAMPLE_RATE = 48000   # wire / client playback rate
ELEVEN_SAMPLE_RATE = 22050   # ElevenLabs output_format=pcm_22050
BYTES_PER_SAMPLE = 2
FRAME_SIZE_MS = 20


@dataclass
class TTSConfig:
    voice_id: str
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.85
    style: float = 0.2
    use_speaker_boost: bool = True
    eleven_sample_rate: int = ELEVEN_SAMPLE_RATE
    output_sample_rate: int = OUTPUT_SAMPLE_RATE
    frame_size_ms: int = FRAME_SIZE_MS


class TTSPipeline:
    def __init__(
        self,
        config: TTSConfig,
        *,
        client: Any = None,
    ):
        self.config = config
        self._client = client
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._closed = False

    # -------------------- public API --------------------

    async def speak(self, text: str) -> None:
        if self._closed or not text or not text.strip():
            return
        try:
            pcm = await asyncio.to_thread(self._synthesize_sync, text)
        except Exception:
            logger.exception("ElevenLabs synthesis failed")
            return
        if not pcm:
            return
        wire_pcm = await asyncio.to_thread(
            self._resample_sync,
            pcm,
            self.config.eleven_sample_rate,
            self.config.output_sample_rate,
        )
        frame_bytes = (
            self.config.output_sample_rate * self.config.frame_size_ms // 1000
        ) * BYTES_PER_SAMPLE
        for start in range(0, len(wire_pcm), frame_bytes):
            chunk = wire_pcm[start : start + frame_bytes]
            if not chunk:
                break
            await self._queue.put(chunk)

    async def stream(self) -> AsyncIterator[bytes]:
        # Drain until the None sentinel from close(). Checking _closed
        # here would skip already-queued frames whenever close() races
        # speak() — the bridge's send task should see every frame that
        # was enqueued before shutdown.
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    # -------------------- internals --------------------

    def _ensure_client(self):
        if self._client is None:
            from scripts.el_client import ElClient
            self._client = ElClient()
        return self._client

    def _synthesize_sync(self, text: str) -> bytes:
        from scripts.el_client import GenerationParams

        client = self._ensure_client()
        params = GenerationParams(
            voice_id=self.config.voice_id,
            text=text,
            model_id=self.config.model_id,
            stability=self.config.stability,
            similarity_boost=self.config.similarity_boost,
            style=self.config.style,
            use_speaker_boost=self.config.use_speaker_boost,
            output_format=f"pcm_{self.config.eleven_sample_rate}",
            sample_rate=self.config.eleven_sample_rate,
        )
        return client.generate_pcm(params)

    @staticmethod
    def _resample_sync(pcm_bytes: bytes, src_sr: int, dst_sr: int) -> bytes:
        if src_sr == dst_sr or not pcm_bytes:
            return pcm_bytes
        samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
        from scipy.signal import resample_poly
        g = gcd(src_sr, dst_sr)
        up = dst_sr // g
        down = src_sr // g
        resampled = resample_poly(samples, up, down)
        clipped = np.clip(resampled, -32768.0, 32767.0).astype("<i2")
        return clipped.tobytes()
