"""
Cloud-side audio bridge (M14).

Listens on a WebSocket for raw int16 PCM mic frames from the OptiPlex,
feeds them into the orchestrator, and streams synthesized TTS PCM back
the other way. No codec on the wire for M15 burn-in — Opus can layer in
later on the RunPod side (apt install libopus0) without changing the
client API.

`websockets` is imported lazily so the module imports cleanly even when
the audio packages aren't installed.

Wire with:
    bridge = CloudAudioBridge(orchestrator, idle_watcher)
    server = await bridge.start(host="0.0.0.0", port=8765)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from .idle_watcher import IdleWatcher


SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SIZE = 960   # 20ms at 48kHz (1920 bytes of int16 PCM per frame)


logger = logging.getLogger("renee.server.audio_bridge")


def _lazy_imports():
    import websockets      # noqa: F401
    return websockets


class CloudAudioBridge:
    def __init__(
        self,
        orchestrator: Any,
        idle_watcher: Optional[IdleWatcher] = None,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_size: int = FRAME_SIZE,
        greet_on_connect: bool = False,
        greeting_prompt: str = "system: greet paul, he just connected",
        safety_layer: Any = None,
        recording_enabled: Optional[bool] = None,
        session_recorder_factory: Optional[Callable[..., Any]] = None,
    ):
        self.orchestrator = orchestrator
        self.idle_watcher = idle_watcher
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = frame_size
        self.greet_on_connect = greet_on_connect
        self.greeting_prompt = greeting_prompt
        # Safety layer is optional (tests supply None). When present it gates
        # new connections against the daily cap cooldown.
        self.safety_layer = safety_layer
        # Fall back to whatever safety_layer the orchestrator or persona
        # core exposes — saves callers from having to thread the layer in
        # twice when there's only one instance in the process.
        if self.safety_layer is None:
            inferred = getattr(orchestrator, "safety_layer", None)
            if inferred is None:
                core = getattr(orchestrator, "persona_core", None)
                inferred = getattr(core, "safety_layer", None) if core else None
            self.safety_layer = inferred
        # Recording wiring (Part 2 deferred -> closed). When `recording_enabled`
        # is None we read RENEE_RECORD from env at handle_client time so each
        # connection is independently re-checked. Tests inject a fake factory
        # so the bridge stays unit-testable without sessions on disk.
        self._recording_enabled_override = recording_enabled
        self._recorder_factory = session_recorder_factory
        self._server = None

    # -------------------- receive (mic -> ASR pipeline) --------------------

    async def _receive_audio(self, ws) -> None:
        feed: Optional[Callable[[bytes], Awaitable[None]]] = getattr(
            self.orchestrator, "feed_audio", None
        )
        warned = False
        async for message in ws:
            if not isinstance(message, (bytes, bytearray)):
                # Text frames carry control messages (set_topic, etc.).
                # Unknown types are silently dropped so the bridge stays
                # forward-compat with future client features.
                self._dispatch_text_message(message)
                continue
            pcm = bytes(message)
            if self.idle_watcher is not None:
                self.idle_watcher.mark_activity()
            if feed is None:
                if not warned:
                    logger.warning(
                        "orchestrator has no feed_audio; draining inbound frames"
                    )
                    warned = True
                continue
            try:
                await feed(pcm)
            except Exception:
                logger.exception("orchestrator.feed_audio raised")

    def _dispatch_text_message(self, raw: Any) -> None:
        """Parse a text frame from the client and dispatch known control
        messages. Failures are swallowed — the bridge must never crash on
        a malformed frame from an untrusted client."""
        try:
            data = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            msg = json.loads(data)
        except Exception:
            logger.debug("ws text frame is not valid JSON; ignoring")
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "set_topic":
            topic = msg.get("text") or msg.get("topic")
            setter = getattr(self.orchestrator, "set_session_topic", None)
            if callable(setter):
                try:
                    setter(topic)
                except Exception:
                    logger.exception("set_session_topic raised")
            else:
                logger.debug("orchestrator lacks set_session_topic; topic ignored")

    # -------------------- send (TTS -> speaker) --------------------

    async def _send_audio(self, ws) -> None:
        stream = getattr(self.orchestrator, "tts_output_stream", None)
        if stream is None:
            # No TTS source yet — keep the task alive until the client
            # disconnects, otherwise asyncio.gather() in handle_client
            # returns immediately and the websocket closes.
            await ws.wait_closed()
            return
        try:
            async for pcm_chunk in stream():
                await ws.send(pcm_chunk)
        except Exception:
            logger.exception("tts_output_stream raised")

    # -------------------- connection handler --------------------

    async def handle_client(self, ws, path: str = "") -> None:
        # Gate on the daily cap cooldown before doing anything else. If the
        # hard stop is still in effect, tell the client what happened and
        # close the socket without touching ASR, TTS, or the orchestrator.
        if self.safety_layer is not None and not self._bridge_allowed():
            cooldown_until = self._cooldown_until()
            farewell = self._cap_farewell()
            payload = {
                "type": "bridge_unavailable",
                "reason": "daily_cap_cooldown",
                "cooldown_until": cooldown_until,
                "message": farewell,
            }
            try:
                await ws.send(json.dumps(payload))
            except Exception:
                logger.debug("cap-cooldown notice send failed", exc_info=True)
            # 1008 is the policy-violation close code; clients interpret it
            # without needing to parse the text frame.
            try:
                await ws.close(code=1008, reason="daily cap cooldown")
            except Exception:
                logger.debug("cap-cooldown close failed", exc_info=True)
            return

        # Install a transcript emitter on the orchestrator for the life of
        # this connection so the mobile client can show what was said and
        # what Renée responded. Binary frames are PCM; text frames are JSON.
        async def _emit(msg: dict) -> None:
            try:
                await ws.send(json.dumps(msg))
            except Exception:
                logger.debug("transcript emit failed", exc_info=True)

        # Prefer the new per-connection registry when the orchestrator
        # exposes it; fall back to the legacy single-slot attribute so
        # this module stays compatible with the minimal fake orchestrators
        # in tests and in scripts/cloud_startup.py.
        unregister: Optional[Callable[[], None]] = None
        register = getattr(
            self.orchestrator, "register_transcript_listener", None
        )
        if callable(register):
            unregister = register(id(ws), _emit)
        elif hasattr(self.orchestrator, "transcript_emitter"):
            prior_emitter = getattr(self.orchestrator, "transcript_emitter", None)
            self.orchestrator.transcript_emitter = _emit

            def unregister() -> None:
                self.orchestrator.transcript_emitter = prior_emitter

        # Recording wiring per-connection (closes the Part 2 deferred item).
        # The recorder taps inbound mic + outbound TTS with bit-for-bit parity
        # via `register_audio_tap` and listens for transcripts via the same
        # listener registry used for the WS emitter. start() mints a session
        # dir under RENEE_SESSIONS_DIR and the QAL chain attestation.
        recorder = self._maybe_start_recorder()
        recorder_audio_unregister: Optional[Callable[[], None]] = None
        recorder_transcript_unregister: Optional[Callable[[], None]] = None
        if recorder is not None:
            register_tap = getattr(self.orchestrator, "register_audio_tap", None)
            if callable(register_tap):
                try:
                    recorder_audio_unregister = register_tap(
                        f"recorder:{id(ws)}",
                        getattr(recorder, "on_mic_pcm", None),
                        getattr(recorder, "on_renee_pcm", None),
                    )
                except Exception:
                    logger.exception("register_audio_tap failed")
            register_listener = getattr(
                self.orchestrator, "register_transcript_listener", None,
            )
            if callable(register_listener):
                try:
                    recorder_transcript_unregister = register_listener(
                        f"recorder-tr:{id(ws)}",
                        getattr(recorder, "on_transcript_async", None),
                    )
                except Exception:
                    logger.exception("recorder transcript register failed")

        # Session-end event: set by the orchestrator when the daily cap
        # trips mid-session (after TTS has had a beat to speak the
        # farewell). The bridge races this against the send/receive tasks
        # and closes the socket cleanly when it fires.
        session_end_event = asyncio.Event()
        install = getattr(self.orchestrator, "install_session_end_event", None)
        if callable(install):
            try:
                install(session_end_event)
            except Exception:
                logger.debug("install_session_end_event raised", exc_info=True)

        receive_task = asyncio.create_task(self._receive_audio(ws))
        send_task = asyncio.create_task(self._send_audio(ws))
        end_task = asyncio.create_task(session_end_event.wait())
        greeting_task: Optional[asyncio.Task] = None
        if self.greet_on_connect:
            greet = getattr(self.orchestrator, "greet_on_connect", None)
            if callable(greet):
                greeting_task = asyncio.create_task(greet(self.greeting_prompt))
            else:
                logger.debug("greet_on_connect enabled but orchestrator lacks hook")
        try:
            # Exit as soon as either side finishes (typically because the
            # websocket closed). Waiting on gather() would hang forever when
            # one side is parked on ws.wait_closed() and the other isn't.
            done, pending = await asyncio.wait(
                {receive_task, send_task, end_task},
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
                exc = t.exception() if not t.cancelled() else None
                if exc is not None:
                    logger.exception("bridge task raised", exc_info=exc)
            # If the end event tripped, explicitly close so the client
            # sees a clean shutdown rather than a dropped socket.
            if session_end_event.is_set():
                try:
                    await ws.close(code=1000, reason="daily cap reached")
                except Exception:
                    logger.debug("cap-trip close failed", exc_info=True)
        except Exception:
            logger.exception("bridge connection error")
        finally:
            for t in (receive_task, send_task, end_task):
                if not t.done():
                    t.cancel()
            if greeting_task is not None and not greeting_task.done():
                greeting_task.cancel()
            clear = getattr(self.orchestrator, "clear_session_end_event", None)
            if callable(clear):
                try:
                    clear()
                except Exception:
                    logger.debug("clear_session_end_event raised", exc_info=True)
            if unregister is not None:
                try:
                    unregister()
                except Exception:
                    logger.debug("transcript unregister raised", exc_info=True)
            if recorder_audio_unregister is not None:
                try:
                    recorder_audio_unregister()
                except Exception:
                    logger.debug("recorder audio unregister raised", exc_info=True)
            if recorder_transcript_unregister is not None:
                try:
                    recorder_transcript_unregister()
                except Exception:
                    logger.debug("recorder transcript unregister raised", exc_info=True)
            if recorder is not None:
                try:
                    recorder.stop()
                except Exception:
                    logger.exception("recorder stop raised")

    # -------------------- recorder helper --------------------

    def _recording_should_run(self) -> bool:
        """Per-connection recording gate. The override on the bridge wins;
        otherwise read RENEE_RECORD from env so flipping the env mid-pod
        between sessions takes effect without bridge restart."""
        if self._recording_enabled_override is not None:
            return bool(self._recording_enabled_override)
        import os as _os
        return _os.environ.get("RENEE_RECORD", "0").strip().lower() in ("1", "true", "yes")

    def _maybe_start_recorder(self) -> Any:
        """Construct + start a SessionRecorder if recording is enabled AND
        the orchestrator exposes the persona_core.identity + memory_store.
        Returns None when any precondition isn't met — the bridge is fully
        functional without a recorder."""
        if not self._recording_should_run():
            return None
        # Identity + memory_store live on persona_core. If the orchestrator
        # is one of the test fakes that doesn't have one, skip.
        core = getattr(self.orchestrator, "persona_core", None)
        identity = getattr(core, "identity", None)
        memory_store = getattr(core, "memory_store", None)
        if identity is None or memory_store is None:
            logger.debug(
                "recording requested but persona_core lacks identity/memory_store; skipping",
            )
            return None
        try:
            if self._recorder_factory is not None:
                rec = self._recorder_factory(
                    agent_identity=identity,
                    memory_store=memory_store,
                    enabled=True,
                )
            else:
                from src.capture.session_recorder import SessionRecorder
                rec = SessionRecorder(
                    agent_identity=identity,
                    memory_store=memory_store,
                    enabled=True,
                )
            session_dir = rec.start()
            if session_dir is None:
                # enabled was True but the recorder declined (already-started, etc.)
                return None
            logger.info("session recorder started: %s", session_dir)
            return rec
        except Exception:
            logger.exception("session recorder failed to start; continuing without")
            return None

    # -------------------- cap gating helpers --------------------

    def _bridge_allowed(self) -> bool:
        """Ask the safety layer whether the bridge can accept new clients."""
        check = getattr(self.safety_layer, "bridge_allowed_now", None)
        if not callable(check):
            return True
        try:
            return bool(check())
        except Exception:
            logger.exception("bridge_allowed_now raised")
            return True  # fail open on unexpected errors

    def _cooldown_until(self) -> Optional[float]:
        get = getattr(self.safety_layer, "bridge_cooldown_until", None)
        if not callable(get):
            return None
        try:
            return get()
        except Exception:
            logger.exception("bridge_cooldown_until raised")
            return None

    def _cap_farewell(self) -> str:
        get = getattr(self.safety_layer, "cap_farewell", None)
        if callable(get):
            try:
                return get()
            except Exception:
                logger.exception("cap_farewell raised")
        return "That's the day. I'll be here tomorrow."

    # -------------------- lifecycle --------------------

    async def start(self, host: str = "0.0.0.0", port: int = 8765):
        websockets = _lazy_imports()
        self._server = await websockets.serve(self.handle_client, host, port)
        logger.info("audio bridge listening on ws://%s:%d (raw PCM)", host, port)
        return self._server

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
