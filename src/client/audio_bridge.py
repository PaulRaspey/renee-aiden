"""
OptiPlex-side audio bridge (M14).

Captures mic audio via sounddevice and streams raw int16 PCM frames
over WebSocket to the cloud pod. In the other direction, receives PCM
frames and plays through the default output. No codec on the wire for
M15 burn-in — Opus layers in later on the RunPod side without changing
this file's behavior.

`sounddevice` and `websockets` are imported lazily so this module
imports on a Python install that doesn't have them yet.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional


SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SIZE = 960   # 20ms at 48kHz (1920 bytes of int16 PCM per frame)


logger = logging.getLogger("renee.client.audio_bridge")


def _lazy_imports():
    import sounddevice     # noqa: F401
    import websockets      # noqa: F401
    return sounddevice, websockets


class ClientAudioBridge:
    def __init__(
        self,
        server_url: str,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_size: int = FRAME_SIZE,
        input_device: Optional[int | str] = None,
        output_device: Optional[int | str] = None,
    ):
        if not server_url.startswith(("ws://", "wss://")):
            raise ValueError(f"server_url must be ws:// or wss://, got: {server_url}")
        self.server_url = server_url
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = frame_size
        self.input_device = input_device
        self.output_device = output_device
        self._running = False

    # -------------------- run --------------------

    async def run(self) -> None:
        sd, websockets = _lazy_imports()

        async with websockets.connect(self.server_url) as ws:
            self._running = True
            logger.info("connected to %s (raw PCM)", self.server_url)
            await asyncio.gather(
                self._send_mic(ws, sd),
                self._receive_speaker(ws, sd),
            )

    def stop(self) -> None:
        self._running = False

    # -------------------- mic -> cloud --------------------

    async def _send_mic(self, ws, sd) -> None:
        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.frame_size,
            device=self.input_device,
        )
        stream.start()
        try:
            while self._running:
                audio, _ = stream.read(self.frame_size)
                await ws.send(audio.tobytes())
        finally:
            stream.stop()
            stream.close()

    # -------------------- cloud -> speaker --------------------

    async def _receive_speaker(self, ws, sd) -> None:
        stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            device=self.output_device,
        )
        stream.start()
        try:
            async for message in ws:
                if not isinstance(message, (bytes, bytearray)):
                    continue
                stream.write(bytes(message))
        finally:
            stream.stop()
            stream.close()
