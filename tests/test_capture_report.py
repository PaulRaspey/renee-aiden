"""Tests for src.capture.report (#62)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.capture.report import ReportInputs, gather, render, write_report


def _make_session(tmp_path: Path, *,
                  manifest: dict | None = None,
                  transcript: list | None = None,
                  triage: dict | None = None,
                  notes: str | None = None,
                  highlights: str | None = None) -> Path:
    sd = tmp_path / "sess-1"
    sd.mkdir()
    if manifest is not None:
        (sd / "session_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if transcript is not None:
        (sd / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    if triage is not None:
        (sd / "triage.json").write_text(json.dumps(triage), encoding="utf-8")
    if notes is not None:
        (sd / "notes.md").write_text(notes, encoding="utf-8")
    if highlights is not None:
        (sd / "highlights.md").write_text(highlights, encoding="utf-8")
    return sd


def test_gather_returns_none_for_missing_artifacts(tmp_path: Path):
    sd = tmp_path / "empty"
    sd.mkdir()
    inputs = gather(sd)
    assert inputs.session_id == "empty"
    assert inputs.manifest is None
    assert inputs.transcript is None
    assert inputs.triage is None
    assert inputs.notes is None
    assert inputs.highlights_md is None


def test_render_minimal_session(tmp_path: Path):
    sd = _make_session(tmp_path, manifest={"start_time": "2026-05-03T20:00:00"})
    md = render(gather(sd))
    assert "# Session report — sess-1" in md
    assert "Started: 2026-05-03T20:00:00" in md
    # Triage section says no results when triage.json is absent
    assert "no triage" in md.lower()


def test_render_includes_topic_from_starter_metadata(tmp_path: Path):
    sd = _make_session(
        tmp_path,
        manifest={
            "starter_metadata": {"topic": "memory consolidation"},
            "presence_score": 4,
            "backend_used": "cascade",
            "pod_id": "pod-x",
        },
    )
    md = render(gather(sd))
    assert "Topic: memory consolidation" in md
    assert "Presence score: 4/5" in md
    assert "Backend: cascade" in md
    assert "Pod: pod-x" in md


def test_render_summarizes_triage_flags(tmp_path: Path):
    triage = {
        "flags": [
            {"category": "fatigue", "severity": "low", "timestamp": "00:01:00",
             "message": "minor pause"},
            {"category": "safety", "severity": "high", "timestamp": "00:05:00",
             "message": "trigger phrase detected"},
            {"category": "safety", "severity": "high", "timestamp": "00:10:00",
             "message": "second trigger"},
            {"category": "prosody", "severity": "medium"},
        ],
        "fatigue_score": 0.42,
    }
    sd = _make_session(tmp_path, triage=triage)
    md = render(gather(sd))
    assert "Flag count: 4" in md
    assert "fatigue: 1" in md
    assert "safety: 2" in md
    assert "prosody: 1" in md
    assert "Fatigue score: 0.42" in md
    # High-severity entries surface inline
    assert "High-severity flags" in md
    assert "trigger phrase detected" in md


def test_render_transcript_summary(tmp_path: Path):
    transcript = [
        {"speaker": "paul", "text": "hi"},
        {"speaker": "renee", "text": "hello"},
        {"speaker": "paul", "text": "how are you"},
        {"speaker": "renee", "text": "i'm here"},
    ]
    sd = _make_session(tmp_path, transcript=transcript)
    md = render(gather(sd))
    assert "4 events (2 user, 2 assistant)" in md
    assert "First: [paul] hi" in md
    assert "Last:  [renee] i'm here" in md


def test_render_includes_notes_and_highlights_verbatim(tmp_path: Path):
    sd = _make_session(
        tmp_path,
        notes="### Manual notes\n- something happened",
        highlights="### Highlights\n- key moment",
    )
    md = render(gather(sd))
    assert "## Highlights" in md
    assert "key moment" in md
    assert "## Notes" in md
    assert "something happened" in md


def test_write_report_persists_to_session_dir(tmp_path: Path):
    sd = _make_session(tmp_path, manifest={"start_time": "x"})
    out = write_report(sd)
    assert out == sd / "report.md"
    assert out.exists()
    assert "# Session report" in out.read_text(encoding="utf-8")


def test_render_handles_missing_manifest_gracefully(tmp_path: Path):
    sd = _make_session(tmp_path)  # no manifest, no anything
    md = render(gather(sd))
    # Just doesn't crash; reports the unknown sections sanely
    assert "# Session report" in md


def test_report_cli_writes_file(tmp_path: Path, monkeypatch, capsys):
    """End-to-end CLI smoke: `renee report sess-1` writes report.md."""
    from src.cli import main as cli_main
    sd = _make_session(tmp_path, manifest={"start_time": "x"})
    monkeypatch.setattr(
        "src.capture.session_recorder.default_sessions_root",
        lambda: tmp_path,
    )
    from types import SimpleNamespace
    args = SimpleNamespace(session_id="sess-1", sessions_root=None, print_only=False)
    rc = cli_main.cmd_report(args)
    assert rc == 0
    assert "wrote" in capsys.readouterr().out
    assert (sd / "report.md").exists()


def test_report_cli_missing_session_returns_1(tmp_path, monkeypatch, capsys):
    from src.cli import main as cli_main
    monkeypatch.setattr(
        "src.capture.session_recorder.default_sessions_root",
        lambda: tmp_path,
    )
    from types import SimpleNamespace
    args = SimpleNamespace(session_id="ghost", sessions_root=None, print_only=False)
    rc = cli_main.cmd_report(args)
    assert rc == 1
    assert "not found" in capsys.readouterr().out
