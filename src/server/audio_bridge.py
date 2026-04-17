"""
Cloud-side audio bridge (M14).

Listens on a WebSocket for Opus-encoded mic frames from the OptiPlex,
decodes them, feeds PCM into the orchestrator, and streams synthesized
TTS back the other way.

Heavy deps (`websockets`, `opuslib`) are imported lazily — the module
imports cleanly even when audio packages aren't installed, so unit
tests and cloud-bridge-unrelated code paths stay importable on PJ's
non-GPU dev box.

Wire with:
    bridge = CloudAudioBridge(orchestrator, idle_watcher)
    server = await bridge.start(host="0.0.0.0", port=8765)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from .idle_watcher import IdleWatcher


SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SIZE = 960   # 20ms at 48kHz


logger = logging.getLogger("renee.server.audio_bridge")


def _lazy_imports():
    """Import opus + websockets only when the bridge actually starts."""
    import websockets      # noqa: F401
    import opuslib         # noqa: F401
    return websockets, opuslib


class CloudAudioBridge:
    def __init__(
        self,
        orchestrator: Any,
        idle_watcher: Optional[IdleWatcher] = None,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_size: int = FRAME_SIZE,
    ):
        self.orchestrator = orchestrator
        self.idle_watcher = idle_watcher
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = frame_size
        self._decoder = None
        self._encoder = None
        self._server = None

    # -------------------- wiring --------------------

    def _ensure_codecs(self) -> None:
        if self._decoder is not None and self._encoder is not None:
            return
        _, opuslib = _lazy_imports()
        self._decoder = opuslib.Decoder(self.sample_rate, self.channels)
        self._encoder = opuslib.Encoder(
            self.sample_rate, self.channels, opuslib.APPLICATION_VOIP
        )

    # -------------------- receive (mic -> ASR pipeline) --------------------

    async def _receive_audio(self, ws) -> None:
        self._ensure_codecs()
        feed: Callable[[bytes], Awaitable[None]] = getattr(
            self.orchestrator, "feed_audio", None
        )
        if feed is None:
            logger.warning("orchestrator has no feed_audio; dropping inbound frames")
            return
        async for message in ws:
            if not isinstance(message, (bytes, bytearray)):
                continue
            try:
                pcm = self._decoder.decode(bytes(message), self.frame_size)
            except Exception:
                logger.exception("opus decode failed")
                continue
            if self.idle_watcher is not None:
                self.idle_watcher.mark_activity()
            try:
                await feed(pcm)
            except Exception:
                logger.exception("orchestrator.feed_audio raised")

    # -------------------- send (TTS -> speaker) --------------------

    async def _send_audio(self, ws) -> None:
        self._ensure_codecs()
        stream = getattr(self.orchestrator, "tts_output_stream", None)
        if stream is None:
            return
        async for pcm_chunk in stream():
            try:
                frame = self._encoder.encode(pcm_chunk, self.frame_size)
            except Exception:
                logger.exception("opus encode failed")
                continue
            await ws.send(frame)

    # -------------------- connection handler --------------------

    async def handle_client(self, ws, path: str = "") -> None:
        receive_task = asyncio.create_task(self._receive_audio(ws))
        send_task = asyncio.create_task(self._send_audio(ws))
        try:
            await asyncio.gather(receive_task, send_task)
        except Exception:
            logger.exception("bridge connection error")
        finally:
            for t in (receive_task, send_task):
                if not t.done():
                    t.cancel()

    # -------------------- lifecycle --------------------

    async def start(self, host: str = "0.0.0.0", port: int = 8765):
        websockets, _ = _lazy_imports()
        self._server = await websockets.serve(self.handle_client, host, port)
        logger.info("audio bridge listening on ws://%s:%d", host, port)
        return self._server

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
