"""
Unit tests for src.voice.xtts_loader.

Exercise everything up to (but not including) the GPU-only `load()` path.
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest
import yaml

from src.voice.xtts_loader import XTTSConfig, XTTSLoader


def _write_wav(path: Path, seconds: float, sr: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n)


@pytest.fixture
def voice_dir(tmp_path, monkeypatch):
    import src.voice.xtts_loader as mod
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    d = tmp_path / "voices" / "renee" / "reference_clips"
    _write_wav(d / "neutral_01.wav", 5.0)
    _write_wav(d / "neutral_02.wav", 6.0)
    _write_wav(d / "warm_01.wav", 4.0)
    (tmp_path / "voices" / "renee" / "metadata.yaml").write_text(
        yaml.safe_dump({
            "voice": "renee",
            "clips": [{"file": "reference_clips/neutral_01.wav"}],
        }),
        encoding="utf-8",
    )
    return tmp_path


def test_preflight_reports_corpus(voice_dir):
    loader = XTTSLoader(voice="renee", config=XTTSConfig(voice="renee", reference_min_seconds=5.0))
    report = loader.preflight()
    assert report["reference_clips"] == 3
    assert report["total_seconds"] == pytest.approx(15.0, abs=0.1)
    assert report["has_metadata"] is True
    assert report["ready_for_load"] is True


def test_preflight_rejects_too_short(voice_dir):
    loader = XTTSLoader(voice="renee", config=XTTSConfig(voice="renee", reference_min_seconds=30.0))
    with pytest.raises(RuntimeError, match="reference corpus"):
        loader.preflight()


def test_preflight_missing_dir(tmp_path, monkeypatch):
    import src.voice.xtts_loader as mod
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    loader = XTTSLoader(voice="ghost")
    with pytest.raises(FileNotFoundError):
        loader.preflight()


def test_reference_wavs_filters_by_register(voice_dir):
    loader = XTTSLoader(voice="renee")
    warm = loader.reference_wavs(registers=["warm"])
    assert len(warm) == 1
    assert warm[0].endswith("warm_01.wav")


def test_reference_wavs_caps_count(voice_dir):
    loader = XTTSLoader(voice="renee", config=XTTSConfig(voice="renee", max_reference_clips=2))
    wavs = loader.reference_wavs()
    assert len(wavs) == 2


def test_load_raises_without_cuda(voice_dir):
    loader = XTTSLoader(voice="renee")
    with pytest.raises(NotImplementedError):
        loader.load()
