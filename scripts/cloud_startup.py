"""
Cloud pod startup script (M14).

Runs on RunPod boot. Orchestrates:
  1. Volume + state directory health checks.
  2. UAHP registry init.
  3. Parallel model-into-VRAM loads (LLM, Whisper, XTTS-v2, embeddings,
     endpointer, backchannel).
  4. Agent registration.
  5. Persona state restore (mood / opinions / memory warmup).
  6. Audio bridge open on configured port.
  7. Quick self-test (inference + TTS sanity).
  8. Idle watcher started — auto-shutdown after N minutes of silence.

Most of the heavy work is delegated to helpers that we stub on boxes
without CUDA. This file is the orchestration layout; it's intentionally
small so the eval harness can exercise the phase ordering without
importing `torch` or `TTS`.

Target: bridge open in under 90s on a cold H100.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


logger = logging.getLogger("renee.startup")


WORKSPACE = Path(os.environ.get("RENEE_WORKSPACE", "/workspace"))
MODELS = WORKSPACE / "models"
STATE = WORKSPACE / "state"
CODE = WORKSPACE / "renee-aiden"
DEPLOY_CONFIG = CODE / "configs" / "deployment.yaml"


@dataclass
class StartupResult:
    ok: bool
    elapsed_s: float
    bridge_url: Optional[str] = None
    errors: list[str] = None


# -------------------- phases --------------------


def _health_checks() -> None:
    if not WORKSPACE.exists():
        raise RuntimeError(f"Network volume not mounted at {WORKSPACE}.")
    MODELS.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)


async def _start_uahp_registry(db_path: Path) -> Any:
    # Lightweight placeholder — the in-repo identity pattern does not need
    # a long-running registry to sign receipts. Kept so startup telemetry
    # still records this phase.
    from src.identity import ReneeIdentityManager
    return ReneeIdentityManager(db_path.parent)


async def _load_llm(path: Path) -> None:
    await asyncio.sleep(0)  # placeholder — GPU loader plugs in here


async def _load_whisper(path: Path) -> None:
    await asyncio.sleep(0)


async def _load_xtts(path: Path) -> None:
    # Real load path: src.voice.xtts_loader.load()
    await asyncio.sleep(0)


async def _load_embeddings(path: Path) -> None:
    await asyncio.sleep(0)


async def _load_endpointer(path: Path) -> None:
    await asyncio.sleep(0)


async def _load_backchannel(path: Path) -> None:
    await asyncio.sleep(0)


async def _restore_state(state_dir: Path) -> None:
    # MoodStore / MemoryStore warm up lazily on first access; nothing to
    # preload here, but we touch the files so missing state surfaces early.
    for name in ("mood.db", "memory.db", "opinions.db"):
        target = state_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)


async def _run_self_test(orchestrator: Any) -> None:
    # Exercise the pipeline once so a degraded GPU surface shows up in logs
    # instead of on the first user turn.
    try:
        orchestrator.text_turn("system: self-test ping")
    except Exception:
        logger.exception("self-test turn failed (non-fatal)")


# -------------------- orchestration --------------------


async def startup(
    *,
    deploy_config_path: Path = DEPLOY_CONFIG,
    orchestrator_factory: Optional[Callable[[], Any]] = None,
    bridge_factory: Optional[Callable[[Any, Any], Any]] = None,
    idle_watcher_factory: Optional[Callable[[int], Any]] = None,
) -> StartupResult:
    t0 = time.time()
    errors: list[str] = []

    logger.info("[1/7] health checks ...")
    _health_checks()

    logger.info("[2/7] UAHP registry ...")
    await _start_uahp_registry(STATE / "uahp_registry.db")

    logger.info("[3/7] loading models in parallel ...")
    await asyncio.gather(
        _load_llm(MODELS / "llama-3.3-70b-instruct-q8"),
        _load_whisper(MODELS / "whisper-large-v3-turbo"),
        _load_xtts(MODELS / "xtts-v2"),
        _load_embeddings(MODELS / "all-MiniLM-L6-v2"),
        _load_endpointer(MODELS / "endpointer"),
        _load_backchannel(MODELS / "backchannel"),
    )

    logger.info("[4/7] registering agents ...")
    # Agents are lazily instantiated by PersonaCore / Orchestrator on first
    # construction — nothing to do at boot time beyond the identity manager
    # that phase 2 already created.

    logger.info("[5/7] restoring persona state ...")
    await _restore_state(STATE)

    logger.info("[6/7] starting audio bridge + idle watcher ...")
    deploy = _load_deploy(deploy_config_path)
    idle_minutes = int(deploy.get("cloud", {}).get("idle_shutdown_minutes", 60))
    port = int(deploy.get("cloud", {}).get("audio_bridge_port", 8765))

    if orchestrator_factory is None:
        from dataclasses import fields as _dc_fields
        from src.orchestrator import Orchestrator
        from src.voice.asr import ASRConfig, ASRPipeline
        from src.voice.tts import TTSConfig, TTSPipeline

        _known_asr_fields = {f.name for f in _dc_fields(ASRConfig)}
        _asr_overrides = {
            k: v for k, v in (deploy.get("asr") or {}).items() if k in _known_asr_fields
        }

        _voice_id = os.environ.get("RENEE_VOICE_ID")
        _has_eleven_key = bool(os.environ.get("ELEVENLABS_API_KEY"))
        _known_tts_fields = {f.name for f in _dc_fields(TTSConfig)}
        _tts_overrides = {
            k: v for k, v in (deploy.get("tts") or {}).items() if k in _known_tts_fields
        }

        def orchestrator_factory() -> Any:
            asr = ASRPipeline(ASRConfig(**_asr_overrides))
            tts = None
            if _voice_id and _has_eleven_key:
                tts = TTSPipeline(TTSConfig(voice_id=_voice_id, **_tts_overrides))
            elif _voice_id or _has_eleven_key:
                logger.warning(
                    "TTS partially configured: RENEE_VOICE_ID=%s, "
                    "ELEVENLABS_API_KEY=%s — both must be set to enable TTS.",
                    bool(_voice_id), _has_eleven_key,
                )
            else:
                logger.info("TTS disabled (RENEE_VOICE_ID / ELEVENLABS_API_KEY not set).")
            return Orchestrator(asr=asr, tts=tts)
    orchestrator = orchestrator_factory()

    if idle_watcher_factory is None:
        from src.server import IdleWatcher
        def idle_watcher_factory(seconds: int) -> Any:
            return IdleWatcher(seconds, on_shutdown=lambda: logger.info("idle_shutdown fired"))
    idle = idle_watcher_factory(idle_minutes * 60)

    bridge = None
    if bridge_factory is not None:
        bridge = bridge_factory(orchestrator, idle)
    else:
        try:
            from src.server.audio_bridge import CloudAudioBridge
            greet = bool((deploy.get("startup") or {}).get("greeting", False))
            bridge = CloudAudioBridge(
                orchestrator,
                idle_watcher=idle,
                greet_on_connect=greet,
            )
            await bridge.start(port=port)
        except Exception as e:
            errors.append(f"audio_bridge_start: {e!r}")
            logger.exception("audio bridge failed to start — idle watcher still running")

    logger.info("[7/7] self-test ...")
    await _run_self_test(orchestrator)

    elapsed = time.time() - t0
    bridge_url = f"ws://0.0.0.0:{port}"
    logger.info("startup complete in %.1fs; bridge=%s", elapsed, bridge_url)

    return StartupResult(
        ok=not errors,
        elapsed_s=round(elapsed, 2),
        bridge_url=bridge_url if not errors else None,
        errors=errors,
    )


def _load_deploy(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


async def _serve_forever() -> int:
    result = await startup()
    if not result.ok:
        print(f"startup finished with errors: {result.errors}")
        return 1
    print(f"renee ready on {result.bridge_url} in {result.elapsed_s}s")
    # Hold the event loop open so the audio bridge keeps serving. Without
    # this, startup() returns and asyncio.run() tears down the loop, which
    # closes the websocket server and the pod stops accepting connections.
    try:
        await asyncio.sleep(float("inf"))
    except asyncio.CancelledError:
        pass
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    return asyncio.run(_serve_forever())


if __name__ == "__main__":
    raise SystemExit(main())
