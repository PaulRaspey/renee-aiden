"""Unit tests for FringeTracer — append-only JSONL writer for A/B eval."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.cognition.fringe_tracer import FringeTracer


# ---- Stub fringe state (only the fields the tracer reads) ----------------


@dataclass
class _StubLoop:
    summary: str
    salience: float = 0.5


@dataclass
class _StubFringe:
    register: np.ndarray = field(
        default_factory=lambda: np.array([0.6, 0.3, 0.1], dtype=np.float32)
    )
    affective_tilt: np.ndarray = field(
        default_factory=lambda: np.array([0.5, 0.0, 0.4, 0.0, 0.0, 0.0], dtype=np.float32)
    )
    temporal_pressure: float = 0.42
    open_loops: list = field(default_factory=lambda: [_StubLoop("first thread"), _StubLoop("second thread")])
    turn_count: int = 3


# ---- Tests ---------------------------------------------------------------


def test_basic_append_creates_file_with_one_line(tmp_path: Path):
    tracer = FringeTracer(base_path=str(tmp_path))
    tracer.trace(
        persona_name="renee",
        turn_id="abc123",
        fringe_state=_StubFringe(),
        prompt_prefix="[Conversational fringe: register tilting technical; ...]",
    )
    today = datetime.now().strftime("%Y-%m-%d")
    expected = tmp_path / f"renee_{today}.jsonl"
    assert expected.exists()
    lines = expected.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # All expected keys present.
    for key in (
        "turn_id", "ts", "persona", "prefix", "dominant_register",
        "register_distribution", "pressure", "n_open_loops",
        "open_loop_summaries", "affect_dominant", "turn_count",
    ):
        assert key in rec, f"missing key: {key}"
    assert rec["turn_id"] == "abc123"
    assert rec["persona"] == "renee"
    assert rec["dominant_register"] == "technical"
    assert rec["pressure"] == pytest.approx(0.42)
    assert rec["n_open_loops"] == 2
    assert rec["affect_dominant"] == ["sharpening", "opening"]
    assert rec["turn_count"] == 3


def test_multiple_appends_same_day_share_one_file(tmp_path: Path):
    tracer = FringeTracer(base_path=str(tmp_path))
    for i in range(3):
        tracer.trace(
            persona_name="renee",
            turn_id=f"turn-{i}",
            fringe_state=_StubFringe(),
            prompt_prefix=f"prefix {i}",
        )
    today = datetime.now().strftime("%Y-%m-%d")
    fp = tmp_path / f"renee_{today}.jsonl"
    lines = fp.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(l)["turn_id"] for l in lines] == ["turn-0", "turn-1", "turn-2"]


def test_date_rollover_creates_separate_files(tmp_path: Path, monkeypatch):
    tracer = FringeTracer(base_path=str(tmp_path))

    class _FrozenDate:
        _now = datetime(2026, 5, 3, 23, 59)

        @classmethod
        def now(cls):
            return cls._now

        @classmethod
        def fromisoformat(cls, s):
            return datetime.fromisoformat(s)

    monkeypatch.setattr("src.cognition.fringe_tracer.datetime", _FrozenDate)

    # Day 1.
    tracer.trace(persona_name="renee", turn_id="d1", fringe_state=_StubFringe(), prompt_prefix="p")
    # Roll the clock to next day.
    _FrozenDate._now = datetime(2026, 5, 4, 0, 1)
    tracer.trace(persona_name="renee", turn_id="d2", fringe_state=_StubFringe(), prompt_prefix="p")

    files = sorted(p.name for p in tmp_path.glob("renee_*.jsonl"))
    assert files == ["renee_2026-05-03.jsonl", "renee_2026-05-04.jsonl"]


def test_per_persona_isolation(tmp_path: Path):
    tracer = FringeTracer(base_path=str(tmp_path))
    tracer.trace(persona_name="renee", turn_id="r1", fringe_state=_StubFringe(), prompt_prefix="p")
    tracer.trace(persona_name="aiden", turn_id="a1", fringe_state=_StubFringe(), prompt_prefix="p")
    today = datetime.now().strftime("%Y-%m-%d")
    renee_file = tmp_path / f"renee_{today}.jsonl"
    aiden_file = tmp_path / f"aiden_{today}.jsonl"
    assert renee_file.exists()
    assert aiden_file.exists()
    assert renee_file != aiden_file
    assert json.loads(renee_file.read_text(encoding="utf-8").strip())["turn_id"] == "r1"
    assert json.loads(aiden_file.read_text(encoding="utf-8").strip())["turn_id"] == "a1"


def test_failure_resilience_does_not_raise(tmp_path: Path, caplog):
    """An unwritable path must produce a WARNING log line and no exception."""
    # Point base_path at a file (not a directory) and write something to it
    # so that mkdir(exist_ok=True) on a path with the same name as a file
    # raises FileExistsError. This mimics a path that is unwritable due to
    # a name collision — Windows-friendly, no permission gymnastics.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")

    tracer = FringeTracer(base_path=str(blocker))
    with caplog.at_level(logging.WARNING, logger="src.cognition.fringe_tracer"):
        tracer.trace(
            persona_name="renee",
            turn_id="fail-1",
            fringe_state=_StubFringe(),
            prompt_prefix="p",
        )
    assert any("trace write failed" in rec.message for rec in caplog.records)


def test_numpy_serialization_roundtrips(tmp_path: Path):
    """np.float32 arrays must serialize cleanly — no TypeError on json.dumps."""
    tracer = FringeTracer(base_path=str(tmp_path))
    state = _StubFringe(
        register=np.array([0.1, 0.2, 0.7], dtype=np.float32),
        affective_tilt=np.array([0.0, 0.0, 0.0, 0.0, 0.5, 0.0], dtype=np.float32),
        temporal_pressure=float(np.float32(0.55)),
    )
    tracer.trace(persona_name="renee", turn_id="np-1", fringe_state=state, prompt_prefix="p")
    today = datetime.now().strftime("%Y-%m-%d")
    rec = json.loads((tmp_path / f"renee_{today}.jsonl").read_text(encoding="utf-8").strip())
    # Floats round-trip cleanly.
    assert rec["register_distribution"] == pytest.approx([0.1, 0.2, 0.7], rel=1e-5)
    assert rec["pressure"] == pytest.approx(0.55, rel=1e-5)
    assert rec["dominant_register"] == "playful"
    assert rec["affect_dominant"] == ["warming"]


def test_every_line_is_parseable_json(tmp_path: Path):
    tracer = FringeTracer(base_path=str(tmp_path))
    for i in range(5):
        tracer.trace(
            persona_name="renee",
            turn_id=f"t-{i}",
            fringe_state=_StubFringe(turn_count=i),
            prompt_prefix=f"prefix {i}",
        )
    today = datetime.now().strftime("%Y-%m-%d")
    fp = tmp_path / f"renee_{today}.jsonl"
    for line in fp.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)  # would raise on malformed JSON
        assert "turn_id" in rec
