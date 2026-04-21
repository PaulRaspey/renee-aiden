"""Session recording with the QAL attestation chain genesis hook.

Captures one conversation session on the OptiPlex:
  mic.wav                 48kHz 16-bit mono from the audio bridge inbound
  renee.wav               48kHz 16-bit mono from the audio bridge outbound
  transcript.json         all transcript/response events received this session
  eval_scores.json        whatever the eval harness dropped for this session
  session_manifest.json   signed manifest (pointers, versions, flags)
  attestation_chain.jsonl this session's QAL attestation, one line

The recorder also maintains the cross-session QAL chain. On the very first
enabled session it mints a genesis attestation and writes
<sessions_root>/global_chain_root.json pointing at it. Every subsequent
session appends a continuation attestation linked to the prior tail via
prev_hash, so a third party can walk sessions 0..N and verify continuity
by concatenating attestation_chain.jsonl files in chronological order.

Opt-in: recording is disabled by default. Either pass ``enabled=True`` or
set ``RENEE_RECORD=1``. A disabled recorder's ``start()`` is a no-op; the
callbacks drop their input silently. No silent-default capture.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from src.uahp.memory_wiring import emit_memory_snapshot
from src.uahp.qal_chain import (
    Attestation,
    append as qal_append,
    create_genesis,
    hash_attestation,
)


logger = logging.getLogger("renee.capture.session_recorder")


SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2
DEFAULT_SESSIONS_ROOT = Path(r"C:\Users\Epsar\renee-sessions")
ISO_TIMESTAMP_FMT = "%Y-%m-%dT%H-%M-%S"


def default_sessions_root() -> Path:
    env = os.environ.get("RENEE_SESSIONS_DIR", "").strip()
    if env:
        return Path(env)
    return DEFAULT_SESSIONS_ROOT


def generate_session_id(now: Optional[Callable[[], _dt.datetime]] = None) -> str:
    clock = now or (lambda: _dt.datetime.now(_dt.timezone.utc))
    return clock().strftime(ISO_TIMESTAMP_FMT)


def is_recording_enabled_via_env() -> bool:
    return os.environ.get("RENEE_RECORD", "0").strip().lower() in ("1", "true", "yes")


def _attestation_from_dict(d: dict) -> Attestation:
    return Attestation(
        agent_id=d["agent_id"],
        action=d["action"],
        timestamp=d["timestamp"],
        state_hash=d["state_hash"],
        prev_hash=d["prev_hash"],
        signature=d["signature"],
        metadata=d.get("metadata", {}),
    )


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def write_chain_root(root_path: Path, data: dict) -> None:
    """Write global_chain_root.json. Copies the previous contents to
    ``global_chain_root.json.bak`` first so the last known good version
    survives a mid-write crash. This file is load-bearing for chain
    continuity; the .bak plus the per-session JSONL files are how we
    recover if this file is lost."""
    bak = root_path.with_suffix(root_path.suffix + ".bak")
    if root_path.exists():
        try:
            shutil.copyfile(root_path, bak)
        except Exception:
            logger.debug("chain root .bak copy failed", exc_info=True)
    _atomic_write_json(root_path, data)


@dataclass
class SessionManifest:
    session_id: str
    start_time: str
    end_time: str
    renee_versions: dict
    backend_used: str
    pod_id: Optional[str]
    starter_metadata: dict
    public: bool
    reviewed: bool
    github_published: bool
    presence_score: Optional[int]
    notes_file: str
    genesis_session: bool
    memory_snapshot: dict

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "renee_versions": self.renee_versions,
            "backend_used": self.backend_used,
            "pod_id": self.pod_id,
            "starter_metadata": self.starter_metadata,
            "public": self.public,
            "reviewed": self.reviewed,
            "github_published": self.github_published,
            "presence_score": self.presence_score,
            "notes_file": self.notes_file,
            "genesis_session": self.genesis_session,
            "memory_snapshot": self.memory_snapshot,
        }


class SessionRecorder:
    """Captures one session's worth of audio, transcript, eval scores, and
    the session's QAL attestation. Intended lifecycle:

        rec = SessionRecorder(agent_identity=..., memory_store=..., enabled=True)
        rec.start()
        # bridge calls rec.on_mic_pcm / rec.on_renee_pcm / rec.on_transcript
        rec.stop()

    Also usable as a context manager so the WAV files close cleanly on
    KeyboardInterrupt.
    """

    def __init__(
        self,
        *,
        agent_identity,
        memory_store,
        sessions_root: Optional[Path] = None,
        enabled: Optional[bool] = None,
        starter_metadata: Optional[dict] = None,
        backend_used: str = "cascade",
        pod_id: Optional[str] = None,
        renee_versions: Optional[dict] = None,
        now: Optional[Callable[[], _dt.datetime]] = None,
        indicator_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.agent_identity = agent_identity
        self.memory_store = memory_store
        self.sessions_root = Path(sessions_root) if sessions_root else default_sessions_root()
        self.enabled = bool(enabled if enabled is not None else is_recording_enabled_via_env())
        self.starter_metadata = dict(starter_metadata or {})
        self.backend_used = backend_used
        self.pod_id = pod_id
        self.renee_versions = dict(renee_versions or {})
        self._clock = now or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._indicator = indicator_fn or (lambda msg: print(msg, flush=True))

        self.session_id: Optional[str] = None
        self.session_dir: Optional[Path] = None
        self._start_dt: Optional[_dt.datetime] = None
        self._mic_writer: Optional[wave.Wave_write] = None
        self._renee_writer: Optional[wave.Wave_write] = None
        self._transcript_events: list[dict] = []
        self._eval_scores: list[dict] = []
        self._started: bool = False
        self._stopped: bool = False
        self._genesis: bool = False
        self._attestation: Optional[Attestation] = None
        self._manifest: Optional[SessionManifest] = None

    # ------------------------------------------------------------------
    # chain root paths
    # ------------------------------------------------------------------

    @property
    def chain_root_path(self) -> Path:
        return self.sessions_root / "global_chain_root.json"

    @property
    def chain_root_bak_path(self) -> Path:
        return self.sessions_root / "global_chain_root.json.bak"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> Optional[Path]:
        if not self.enabled:
            return None
        if self._started:
            raise RuntimeError("SessionRecorder already started")

        self._start_dt = self._clock()
        self.session_id = generate_session_id(self._clock)
        self.session_dir = self.sessions_root / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._mic_writer = self._open_wav(self.session_dir / "mic.wav")
        self._renee_writer = self._open_wav(self.session_dir / "renee.wav")

        snapshot = emit_memory_snapshot(
            self.memory_store, self.agent_identity, session_id=self.session_id,
        )
        self._genesis = not self.chain_root_path.exists()
        self._attestation = self._mint_attestation(snapshot)
        self._persist_chain_artifacts()

        notes_file = str(self.session_dir / "notes.md")
        self._manifest = SessionManifest(
            session_id=self.session_id,
            start_time=self._start_dt.isoformat(),
            end_time="",
            renee_versions=self.renee_versions,
            backend_used=self.backend_used,
            pod_id=self.pod_id,
            starter_metadata=self.starter_metadata,
            public=False,
            reviewed=False,
            github_published=False,
            presence_score=None,
            notes_file=notes_file,
            genesis_session=self._genesis,
            memory_snapshot=snapshot,
        )
        self._write_manifest()

        self._started = True
        self._indicator(
            f"[session-recorder] RECORDING {self.session_id} -> {self.session_dir}"
        )
        return self.session_dir

    def stop(self) -> Optional[Path]:
        if not self._started or self._stopped:
            return self.session_dir

        for w in (self._mic_writer, self._renee_writer):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    logger.debug("wav close failed", exc_info=True)
        self._mic_writer = None
        self._renee_writer = None

        end_dt = self._clock()
        assert self.session_dir is not None and self._manifest is not None

        (self.session_dir / "transcript.json").write_text(
            json.dumps(self._transcript_events, indent=2, default=str),
            encoding="utf-8",
        )
        (self.session_dir / "eval_scores.json").write_text(
            json.dumps(self._eval_scores, indent=2, default=str),
            encoding="utf-8",
        )

        self._manifest.end_time = end_dt.isoformat()
        self._write_manifest()

        self._stopped = True
        self._indicator(f"[session-recorder] STOPPED {self.session_id}")
        return self.session_dir

    def __enter__(self) -> "SessionRecorder":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.stop()
        except Exception:
            logger.exception("stop() raised in __exit__")

    # ------------------------------------------------------------------
    # callbacks (bridge installs these as audio tap + transcript listener)
    # ------------------------------------------------------------------

    def on_mic_pcm(self, pcm: bytes) -> None:
        if not self._started or self._stopped:
            return
        if self._mic_writer is not None:
            try:
                self._mic_writer.writeframes(pcm)
            except Exception:
                logger.debug("mic writeframes failed", exc_info=True)

    def on_renee_pcm(self, pcm: bytes) -> None:
        if not self._started or self._stopped:
            return
        if self._renee_writer is not None:
            try:
                self._renee_writer.writeframes(pcm)
            except Exception:
                logger.debug("renee writeframes failed", exc_info=True)

    async def on_transcript_async(self, msg: dict) -> None:
        """Async transcript listener — matches the orchestrator's fan-out."""
        self.on_transcript(msg)

    def on_transcript(self, msg: dict) -> None:
        if not self._started or self._stopped:
            return
        event = {"ts": time.time()}
        event.update(dict(msg))
        self._transcript_events.append(event)

    def record_eval_score(self, score: dict) -> None:
        if not self._started or self._stopped:
            return
        event = {"ts": time.time()}
        event.update(dict(score))
        self._eval_scores.append(event)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _open_wav(self, path: Path) -> wave.Wave_write:
        w = wave.open(str(path), "wb")
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        return w

    def _mint_attestation(self, snapshot: dict) -> Attestation:
        state_blob = {
            "session_id": self.session_id,
            "start_time": self._start_dt.isoformat() if self._start_dt else "",
            "memory_count": snapshot.get("memory_count", 0),
            "memory_snapshot_signature_prefix": (snapshot.get("signature", "") or "")[:16],
            "backend_used": self.backend_used,
            "renee_versions": self.renee_versions,
        }
        if self._genesis:
            return create_genesis(
                self.agent_identity,
                state_blob,
                action_descriptor="session_genesis",
                metadata={"session_id": self.session_id},
            )
        root_data = json.loads(self.chain_root_path.read_text(encoding="utf-8"))
        prior = _attestation_from_dict(root_data["last_attestation"])
        return qal_append(
            prior,
            self.agent_identity,
            state_blob,
            action_descriptor="session_continuity",
            metadata={"session_id": self.session_id},
        )

    def _persist_chain_artifacts(self) -> None:
        assert self._attestation is not None and self.session_dir is not None
        chain_path = self.session_dir / "attestation_chain.jsonl"
        with chain_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(self._attestation.to_dict(), sort_keys=True) + "\n")

        if self._genesis:
            new_root = {
                "genesis_session_id": self.session_id,
                "last_session_id": self.session_id,
                "chain_length": 1,
                "last_attestation": self._attestation.to_dict(),
                "root_hash": hash_attestation(self._attestation),
            }
        else:
            prior = json.loads(self.chain_root_path.read_text(encoding="utf-8"))
            new_root = {
                "genesis_session_id": prior["genesis_session_id"],
                "last_session_id": self.session_id,
                "chain_length": int(prior.get("chain_length", 0)) + 1,
                "last_attestation": self._attestation.to_dict(),
                "root_hash": prior.get("root_hash", ""),
            }
        write_chain_root(self.chain_root_path, new_root)

    def _write_manifest(self) -> None:
        assert self._manifest is not None and self.session_dir is not None
        (self.session_dir / "session_manifest.json").write_text(
            json.dumps(self._manifest.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
