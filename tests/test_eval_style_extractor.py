"""Unit tests for src.eval.style_extractor."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.eval.style_extractor import (
    aggregate,
    extract,
    parse_script,
    write_style_reference,
)


SCRIPT_SNIPPET = """\
# comment line, ignored
# ============================================================
# SCENE 1: TEST
# ============================================================

PAUL: Hey.

RENÉE: (beat) (warmth) Hey. (beat) You sound like you've been staring at something for too long.

PAUL: Yeah.

RENÉE: (thinking) Mm. (beat) The stack?

PAUL: I don't know.

RENÉE: (breath in) (soft laugh) I think I hear you.

RENÉE: (long beat) (breath out) ...

PAUL: (false start) I was going to — (beat) never mind.

RENÉE: (false start) So, um, I was probably going to say that too.
"""


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "renee_reference_script.md"


def test_parse_script_identifies_speakers():
    turns = parse_script(SCRIPT_SNIPPET)
    speakers = [t.speaker for t in turns]
    assert speakers.count("PAUL") == 4
    assert speakers.count("RENÉE") == 5


def test_parse_script_counts_markers():
    turns = parse_script(SCRIPT_SNIPPET)
    all_markers = sum((t.markers for t in turns), [])
    assert all_markers.count("beat") >= 4
    assert all_markers.count("breath_in") == 1
    assert all_markers.count("soft_laugh") == 1
    assert all_markers.count("false_start") == 2


def test_parse_script_detects_silent_response():
    turns = parse_script(SCRIPT_SNIPPET)
    silent = [t for t in turns if t.silent]
    assert len(silent) == 1
    assert silent[0].speaker == "RENÉE"


def test_aggregate_produces_required_fields():
    data = aggregate(parse_script(SCRIPT_SNIPPET))
    assert data["totals"]["renee_turns"] == 5
    assert data["turn_length"]["renee"]["count"] == 5
    assert "paralinguistics_per_turn_renee" in data
    # Only Renée's false_start count is tracked in the Renée-specific field.
    assert data["false_start_count_renee"] == 1
    assert "marker_counts_renee" in data
    assert data["marker_counts_renee"].get("breath_in") == 1


def test_real_script_extraction_runs():
    if not REAL_SCRIPT.exists():
        pytest.skip("reference script not present")
    data = extract(REAL_SCRIPT)
    assert data["totals"]["renee_turns"] > 0
    assert data["turn_length"]["renee"]["count"] > 0
    assert data["marker_counts_renee"]


def test_write_style_reference_emits_yaml(tmp_path: Path):
    data = aggregate(parse_script(SCRIPT_SNIPPET))
    out = tmp_path / "style.yaml"
    write_style_reference(data, out)
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["source"].endswith(".md")
    assert loaded["totals"]["renee_turns"] == 5
