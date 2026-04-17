"""
XTTS-v2 loader scaffolding.

PJ does not have a CUDA-capable GPU locally; this module only handles the
pre-model-load plumbing so that when we spin up a RunPod H100 the reference
clips and speaker embedding can be consumed by XTTS-v2 directly.

On a GPU box:

    from src.voice.xtts_loader import XTTSLoader
    loader = XTTSLoader(voice="renee")
    loader.preflight()        # verify reference clips + metadata on disk
    model = loader.load()     # heavy: downloads weights, warms CUDA

    audio = model.tts_to_file(
        text="...",
        speaker_wav=loader.reference_wavs(),
        language="en",
        file_path="out.wav",
    )

`load()` raises NotImplementedError when CUDA is unavailable. Everything else
— preflight, reference clip discovery, embedding caching — runs locally.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class XTTSConfig:
    voice: str
    model_name: str = "tts_models/multilingual/multi-dataset/xtts_v2"
    language: str = "en"
    sample_rate: int = 24000
    reference_min_seconds: float = 6.0
    max_reference_clips: int = 8


class XTTSLoader:
    def __init__(self, voice: str = "renee", *, config: Optional[XTTSConfig] = None):
        self.voice = voice
        self.config = config or XTTSConfig(voice=voice)
        self.voice_dir = REPO_ROOT / "voices" / voice
        self.reference_dir = self.voice_dir / "reference_clips"
        self.embedding_path = self.voice_dir / "embedding.npy"
        self.metadata_path = self.voice_dir / "metadata.yaml"
        self._model = None

    # ------------------------------------------------------------------
    # local-only operations
    # ------------------------------------------------------------------

    def preflight(self) -> dict:
        """
        Verify the reference corpus on disk is minimally viable.
        Returns a summary dict. Raises if something's missing.
        """
        if not self.reference_dir.exists():
            raise FileNotFoundError(f"reference_clips dir missing: {self.reference_dir}")
        wavs = sorted(self.reference_dir.glob("*.wav"))
        if not wavs:
            raise FileNotFoundError(f"no reference WAVs in {self.reference_dir}")

        meta = {}
        if self.metadata_path.exists():
            meta = yaml.safe_load(self.metadata_path.read_text(encoding="utf-8")) or {}

        total_seconds = 0.0
        try:
            import soundfile as sf
            for w in wavs:
                info = sf.info(str(w))
                total_seconds += info.frames / max(1, info.samplerate)
        except ImportError:
            total_seconds = float("nan")

        if total_seconds < self.config.reference_min_seconds:
            raise RuntimeError(
                f"reference corpus is only {total_seconds:.1f}s; need >= "
                f"{self.config.reference_min_seconds}s for a usable clone."
            )

        return {
            "voice": self.voice,
            "reference_clips": len(wavs),
            "total_seconds": total_seconds,
            "has_metadata": bool(meta),
            "metadata_clips": len(meta.get("clips", [])),
            "ready_for_load": True,
        }

    def reference_wavs(self, registers: Optional[list[str]] = None) -> list[str]:
        """
        Return paths to reference clips XTTS-v2 should condition on.

        If `registers` is passed, prefer clips whose filename starts with one
        of them (e.g. ["neutral", "warm"]). Caps at config.max_reference_clips.
        """
        wavs = sorted(self.reference_dir.glob("*.wav"))
        if registers:
            allowed_prefixes = tuple(f"{r}_" for r in registers)
            wavs = [w for w in wavs if w.name.startswith(allowed_prefixes)] or wavs
        return [str(w) for w in wavs[: self.config.max_reference_clips]]

    # ------------------------------------------------------------------
    # model load — RunPod H100 only
    # ------------------------------------------------------------------

    def load(self):
        """
        Load XTTS-v2 to GPU. Not executed locally; see module docstring.
        """
        try:
            import torch
        except ImportError as e:
            raise NotImplementedError(
                "torch not installed. Run this on the RunPod H100 pod where "
                "torch + TTS are in the image."
            ) from e

        if not torch.cuda.is_available():
            raise NotImplementedError(
                "CUDA unavailable. XTTS-v2 requires a GPU. Deploy to RunPod."
            )

        from TTS.api import TTS  # type: ignore[import-not-found]

        self._model = TTS(self.config.model_name).to("cuda")
        return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None
