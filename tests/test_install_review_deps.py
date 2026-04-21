"""Tests for the review deps idempotency + warning logic.

Does not exercise pip or actually install anything. Covers the pure
Python helpers the .bat wrapper calls to decide what to skip, what to
download, and what to warn about.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.capture import review_deps


def test_check_installed_detects_stdlib():
    assert review_deps.check_installed("json") is True


def test_check_installed_false_for_unknown_name():
    assert review_deps.check_installed("nonexistent_xyz_abc_9876543210") is False


def test_status_all_returns_every_review_dep():
    statuses = review_deps.status_all()
    names = {s.spec.pkg for s in statuses}
    assert {"whisperx", "praat-parselmouth", "pyannote.audio", "matplotlib", "plotly"} <= names


def test_missing_deps_listing(monkeypatch):
    monkeypatch.setattr(
        review_deps,
        "check_installed",
        lambda name: name != "whisperx",
    )
    missing = review_deps.missing_deps()
    assert len(missing) == 1
    assert missing[0].spec.pkg == "whisperx"


def test_missing_deps_empty_when_all_installed(monkeypatch):
    monkeypatch.setattr(review_deps, "check_installed", lambda name: True)
    assert review_deps.missing_deps() == []


def test_check_hf_token_missing(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    ok, msg = review_deps.check_hf_token()
    assert ok is False
    assert "pyannote" in msg.lower()
    assert "huggingface" in msg.lower()
    assert "https://huggingface.co" in msg


def test_check_hf_token_present_via_hf_token(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_fake_token_value_123")
    ok, _ = review_deps.check_hf_token()
    assert ok is True


def test_check_hf_token_present_via_legacy_env(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "hub_fake_token")
    ok, _ = review_deps.check_hf_token()
    assert ok is True


def test_check_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(review_deps.shutil, "which", lambda name: None)
    ok, msg = review_deps.check_ffmpeg()
    assert ok is False
    assert "ffmpeg" in msg.lower()
    assert "install" in msg.lower()
    assert "gyan.dev" in msg or "chocolatey" in msg.lower()


def test_check_ffmpeg_present(monkeypatch):
    monkeypatch.setattr(
        review_deps.shutil, "which", lambda name: r"C:\ffmpeg\bin\ffmpeg.exe",
    )
    ok, _ = review_deps.check_ffmpeg()
    assert ok is True


def test_estimated_download_includes_whisper_model():
    all_missing = review_deps.status_all()
    total = review_deps.estimated_download_mb(all_missing, whisper_model="base.en")
    assert total >= 150 + sum(s.spec.size_mb for s in all_missing)


def test_estimated_download_grows_with_larger_whisper_model():
    all_missing = review_deps.status_all()
    base_total = review_deps.estimated_download_mb(all_missing, whisper_model="base.en")
    large_total = review_deps.estimated_download_mb(all_missing, whisper_model="large-v3")
    assert large_total > base_total


def test_summary_exits_zero_when_nothing_to_install(monkeypatch, capsys):
    monkeypatch.setattr(review_deps, "check_installed", lambda name: True)
    monkeypatch.setenv("HF_TOKEN", "fake")
    monkeypatch.setattr(
        review_deps.shutil, "which", lambda name: r"C:\ffmpeg\bin\ffmpeg.exe",
    )
    assert review_deps.main(["summary"]) == 0
    captured = capsys.readouterr()
    assert "nothing to do" in captured.out


def test_summary_exits_two_when_work_remains(monkeypatch):
    monkeypatch.setattr(review_deps, "check_installed", lambda name: False)
    assert review_deps.main(["summary"]) == 2


def test_summary_prints_warnings_when_hf_and_ffmpeg_missing(monkeypatch, capsys):
    monkeypatch.setattr(review_deps, "check_installed", lambda name: False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setattr(review_deps.shutil, "which", lambda name: None)
    review_deps.main(["summary"])
    captured = capsys.readouterr()
    assert "MISSING" in captured.out
    assert "HuggingFace" in captured.out or "HF_TOKEN" in captured.out
    assert "ffmpeg" in captured.out.lower()


def test_missing_cli_emits_plain_pkg_names(monkeypatch, capsys):
    monkeypatch.setattr(
        review_deps,
        "check_installed",
        lambda name: name not in ("whisperx", "plotly"),
    )
    review_deps.main(["missing"])
    captured = capsys.readouterr()
    lines = [ln.strip() for ln in captured.out.splitlines() if ln.strip()]
    assert "whisperx" in lines
    assert "plotly" in lines
    assert "matplotlib" not in lines


def test_check_hf_cli_returns_one_when_missing(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    assert review_deps.main(["check-hf"]) == 1


def test_check_hf_cli_returns_zero_when_present(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "x")
    assert review_deps.main(["check-hf"]) == 0


def test_check_ffmpeg_cli_returns_one_when_missing(monkeypatch):
    monkeypatch.setattr(review_deps.shutil, "which", lambda name: None)
    assert review_deps.main(["check-ffmpeg"]) == 1


def test_check_ffmpeg_cli_returns_zero_when_present(monkeypatch):
    monkeypatch.setattr(
        review_deps.shutil, "which", lambda name: r"C:\ffmpeg\bin\ffmpeg.exe",
    )
    assert review_deps.main(["check-ffmpeg"]) == 0


def test_install_bat_exists_with_expected_structure():
    bat = Path(__file__).resolve().parent.parent / "scripts" / "install_review_deps.bat"
    assert bat.exists()
    content = bat.read_text(encoding="utf-8", errors="ignore")
    assert ".venv" in content
    assert "pip install" in content
    assert "review_deps" in content
    assert "ffmpeg" in content.lower()
