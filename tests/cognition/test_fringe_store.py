"""Unit tests for FringeStore — load, save, decay-on-load, dim mismatch."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.cognition.fringe import FringeState, OpenLoop
from src.cognition.fringe_store import FringeStore


def test_load_returns_fresh_when_file_missing(tmp_path: Path):
    fs = FringeStore("renee", tmp_path)
    state = fs.load(embedding_dim=384)
    assert state.turn_count == 0
    assert state.embedding_dim == 384


def test_save_then_load_roundtrip(tmp_path: Path):
    fs = FringeStore("renee", tmp_path)
    state = FringeState(embedding_dim=8)
    state.topical_vector = np.linspace(0, 1, 8, dtype=np.float32)
    state.turn_count = 7
    state.temporal_pressure = 0.42
    state.open_loops = [OpenLoop("l1", 0.9, 5, "fringe thread")]
    fs.save(state)
    assert fs.path.exists()

    restored = fs.load(embedding_dim=8)
    assert restored.turn_count == 7
    assert restored.embedding_dim == 8
    assert pytest.approx(restored.temporal_pressure, rel=1e-3) == 0.42
    assert len(restored.open_loops) == 1
    assert restored.open_loops[0].summary == "fringe thread"


def test_load_applies_decay_to_now(tmp_path: Path):
    fs = FringeStore("renee", tmp_path)
    state = FringeState(embedding_dim=8)
    state.topical_vector = np.ones(8, dtype=np.float32)
    state.temporal_pressure = 0.8
    state.last_updated = datetime.now() - timedelta(hours=24)
    fs.save(state)

    restored = fs.load(embedding_dim=8)
    # 0.95**24 ≈ 0.292, so values should be attenuated.
    assert float(restored.topical_vector[0]) < 0.5
    assert restored.temporal_pressure < 0.5


def test_per_persona_isolation(tmp_path: Path):
    """Two personas must not collide in the same state_dir."""
    fs_renee = FringeStore("renee", tmp_path)
    fs_aiden = FringeStore("aiden", tmp_path)

    s_renee = FringeState(embedding_dim=8)
    s_renee.turn_count = 11
    fs_renee.save(s_renee)

    s_aiden = FringeState(embedding_dim=8)
    s_aiden.turn_count = 22
    fs_aiden.save(s_aiden)

    assert fs_renee.load(embedding_dim=8).turn_count == 11
    assert fs_aiden.load(embedding_dim=8).turn_count == 22
    assert fs_renee.path != fs_aiden.path


def test_dim_mismatch_keeps_persisted_state(tmp_path: Path):
    """Loading with a different expected dim should still return the
    persisted state (with a logged warning) rather than dropping it."""
    fs = FringeStore("renee", tmp_path)
    state = FringeState(embedding_dim=8)
    state.turn_count = 4
    fs.save(state)

    restored = fs.load(embedding_dim=384)
    # turn_count survives; embedding_dim retains the persisted value.
    assert restored.turn_count == 4
    assert restored.embedding_dim == 8


def test_corrupt_file_returns_fresh_state(tmp_path: Path):
    fs = FringeStore("renee", tmp_path)
    fs.path.write_text("{not valid json", encoding="utf-8")
    state = fs.load(embedding_dim=384)
    assert state.turn_count == 0


def test_save_is_atomic(tmp_path: Path):
    """A successful save leaves no .tmp file behind."""
    fs = FringeStore("renee", tmp_path)
    state = FringeState()
    fs.save(state)
    tmp = fs.path.with_suffix(fs.path.suffix + ".tmp")
    assert not tmp.exists()
    assert fs.path.exists()
