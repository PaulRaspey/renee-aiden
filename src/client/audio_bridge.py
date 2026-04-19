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

import numpy as np


SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SIZE = 960   # 20ms at 48kHz (1920 bytes of int16 PCM per frame)
JITTER_BUFFER_CHUNKS = 4   # prime ~80ms of audio before playback starts
JITTER_QUEUE_MAX = 128     # hard cap on queued chunks (~2.5s) — if we fall
                           # this far behind, drop the oldest rather than
                           # grow the buffer unbounded and widen latency


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
        # Outer loop handles "no input device yet" and mid-session device
        # loss without killing the websocket — the previous version raised
        # on InputStream() or read(), which canceled _receive_speaker via
        # asyncio.gather and dropped the whole connection.
        while self._running:
            stream = await self._open_stream(sd.InputStream, "mic")
            if stream is None:  # _running flipped while we were waiting
                return
            try:
                while self._running:
                    try:
                        audio, _ = stream.read(self.frame_size)
                    except Exception:
                        logger.warning("mic read failed; reopening stream", exc_info=True)
                        break
                    # Websocket errors are fatal — let them propagate so
                    # gather() tears the connection down cleanly.
                    await ws.send(audio.tobytes())
            finally:
                _safe_close(stream)

    # -------------------- cloud -> speaker --------------------

    async def _receive_speaker(self, ws, sd) -> None:
        while self._running:
            stream = await self._open_stream(sd.OutputStream, "speaker")
            if stream is None:
                return
            # Producer/consumer with an asyncio queue and underrun recovery:
            # receive frames into the queue as fast as they arrive; drain to
            # the device from a separate coroutine. If the queue empties we
            # re-prime before resuming so a transient TCP stall doesn't
            # propagate as a choppy cut-in.
            queue: asyncio.Queue = asyncio.Queue(maxsize=JITTER_QUEUE_MAX)
            _SENTINEL: object = object()

            async def _producer() -> None:
                try:
                    async for message in ws:
                        if not isinstance(message, (bytes, bytearray)):
                            continue
                        audio = np.frombuffer(message, dtype=np.int16)
                        # Drop the oldest frame if we've fallen far behind —
                        # growing the queue past JITTER_QUEUE_MAX would just
                        # add permanent latency with no audible benefit.
                        if queue.full():
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                        await queue.put(audio)
                finally:
                    await queue.put(_SENTINEL)

            async def _consumer() -> None:
                primed = False
                while True:
                    if not primed:
                        pending: list = []
                        while len(pending) < JITTER_BUFFER_CHUNKS:
                            chunk = await queue.get()
                            if chunk is _SENTINEL:
                                return
                            pending.append(chunk)
                        for chunk in pending:
                            await asyncio.to_thread(stream.write, chunk)
                        primed = True
                        continue
                    try:
                        chunk = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        # Underrun — drop back into priming so we rebuild
                        # the safety backlog before the next write.
                        primed = False
                        continue
                    if chunk is _SENTINEL:
                        return
                    await asyncio.to_thread(stream.write, chunk)

            producer_task = asyncio.create_task(_producer())
            consumer_task = asyncio.create_task(_consumer())
            try:
                done, pending = await asyncio.wait(
                    {producer_task, consumer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                for t in done:
                    exc = t.exception()
                    if exc is not None:
                        logger.warning(
                            "speaker task raised; reopening stream",
                            exc_info=exc,
                        )
                return  # websocket closed (producer finished first, naturally)
            finally:
                _safe_close(stream)

    # -------------------- helpers --------------------

    async def _open_stream(self, factory, label: str, *, retry_s: float = 5.0):
        kwargs = dict(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            device=self.input_device if label == "mic" else self.output_device,
        )
        if label == "mic":
            kwargs["blocksize"] = self.frame_size
        warned = False
        while self._running:
            try:
                stream = factory(**kwargs)
                stream.start()
                if warned:
                    logger.info("%s stream now available", label)
                return stream
            except Exception as e:
                if not warned:
                    logger.warning("%s unavailable (%s); waiting for a device …", label, e)
                    warned = True
                else:
                    logger.debug("%s still unavailable (%s)", label, e)
                await asyncio.sleep(retry_s)
        return None


def _safe_close(stream) -> None:
    try:
        stream.stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass
