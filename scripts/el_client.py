"""
ElevenLabs REST helper used by the M5 reference-corpus generator and the M6
paralinguistic library generator.

Keeps one shared place for: API key load, output-format selection, PCM → WAV
save, retry/backoff, and on-disk resume (skip clips that already exist).
"""
from __future__ import annotations

import os
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from elevenlabs import ElevenLabs
    from elevenlabs.core.api_error import ApiError
except ImportError as e:
    print(f"elevenlabs sdk not installed: {e}", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env")


@dataclass
class GenerationParams:
    voice_id: str
    text: str
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.85
    style: float = 0.2
    use_speaker_boost: bool = True
    output_format: str = "pcm_24000"
    sample_rate: int = 24000
    seed: Optional[int] = None


class ElClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.getenv("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError(
                "ELEVENLABS_API_KEY missing. Set it in .env or the environment."
            )
        self.client = ElevenLabs(api_key=key)

    def generate_pcm(self, params: GenerationParams, max_retries: int = 6) -> bytes:
        """Return raw little-endian 16-bit PCM at params.sample_rate."""
        from elevenlabs.types import VoiceSettings

        settings = VoiceSettings(
            stability=params.stability,
            similarity_boost=params.similarity_boost,
            style=params.style,
            use_speaker_boost=params.use_speaker_boost,
        )

        last_err: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                kwargs = dict(
                    voice_id=params.voice_id,
                    text=params.text,
                    model_id=params.model_id,
                    output_format=params.output_format,
                    voice_settings=settings,
                )
                if params.seed is not None:
                    kwargs["seed"] = params.seed
                stream = self.client.text_to_speech.convert(**kwargs)
                return b"".join(stream)
            except ApiError as e:
                last_err = e
                status = getattr(e, "status_code", None)
                body = getattr(e, "body", {}) or {}
                detail = str(body).lower()
                if status == 429:
                    wait = 2 ** (attempt + 1)
                    print(f"  429 rate limit, backing off {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if status is not None and 500 <= status < 600:
                    wait = 2 ** (attempt + 1)
                    print(f"  {status} upstream error, backing off {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if ("model_not_found" in detail or "not available" in detail) and attempt == 0:
                    print(f"  model {params.model_id} unavailable, retrying with eleven_multilingual_v2", file=sys.stderr)
                    params.model_id = "eleven_multilingual_v2"
                    continue
                if "output_format" in detail and attempt == 0:
                    print("  falling back to pcm_24000", file=sys.stderr)
                    params.output_format = "pcm_24000"
                    params.sample_rate = 24000
                    continue
                raise
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                print(f"  {type(e).__name__}: {e}, retry in {wait}s", file=sys.stderr)
                time.sleep(wait)

        raise RuntimeError(f"generation failed after {max_retries} retries: {last_err}")


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int, out_path: Path) -> None:
    """Write raw 16-bit mono PCM to a WAV file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)


def pcm_to_numpy(pcm_bytes: bytes) -> np.ndarray:
    """16-bit signed int little-endian to float32 [-1, 1]."""
    arr = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
    return arr / 32768.0


def numpy_to_wav(arr: np.ndarray, sample_rate: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if arr.dtype != np.int16:
        clipped = np.clip(arr, -1.0, 1.0)
        arr16 = (clipped * 32767.0).astype(np.int16)
    else:
        arr16 = arr
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(arr16.tobytes())


def trim_silence(audio: np.ndarray, sample_rate: int, top_db: float = 30.0, pad_ms: int = 40) -> np.ndarray:
    """Trim leading/trailing silence. Keep a small pad on each side so attack/release survive."""
    import librosa

    trimmed, _ = librosa.effects.trim(audio, top_db=top_db, frame_length=1024, hop_length=256)
    if trimmed.size == 0:
        return audio
    pad = int(sample_rate * pad_ms / 1000)
    # Re-pad using original audio if there's room around the trim.
    start = max(0, int(np.where(audio == trimmed[0])[0][0]) - pad) if trimmed.size else 0
    end = min(audio.size, start + trimmed.size + 2 * pad)
    return audio[start:end]


def loudnorm(audio: np.ndarray, sample_rate: int, target_lufs: float = -23.0) -> np.ndarray:
    """Loudness normalize to a target LUFS. Returns float32."""
    import pyloudnorm as pyln

    meter = pyln.Meter(sample_rate)
    if audio.size < sample_rate // 2:  # too short for LUFS, just peak-normalize
        peak = float(np.max(np.abs(audio)) + 1e-9)
        return audio * (0.9 / peak)
    try:
        loudness = meter.integrated_loudness(audio)
        return pyln.normalize.loudness(audio, loudness, target_lufs).astype(np.float32)
    except Exception:
        peak = float(np.max(np.abs(audio)) + 1e-9)
        return audio * (0.9 / peak)


def existing_clips(directory: Path, pattern: str = "*.wav") -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob(pattern))
