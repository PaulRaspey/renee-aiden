"""Unit tests for FringeState — initialization, update, decay, persistence."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from src.cognition.fringe import FringeState, OpenLoop, Turn


# ---- Mocks for the update() dependencies ----------------------------------


class _StubEmbedder:
    def __init__(self, dim: int = 384, fixed: float = 0.5):
        self.dim = dim
        self.fixed = fixed

    def embed(self, text: str) -> np.ndarray:
        # Deterministic vector: hash text into a stable direction so
        # different texts produce different but reproducible vectors.
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.standard_normal(self.dim).astype(np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
        return v


class _StubAffect:
    def score(self, turn):
        return np.array([0.5, 0.0, 0.4, 0.0, 0.6, 0.0], dtype=np.float32)


class _StubRegister:
    def detect(self, turn):
        return np.array([0.7, 0.2, 0.1], dtype=np.float32)


class _StubLoopNone:
    def check(self, turn, turn_count):
        return None


class _StubLoopAlways:
    def check(self, turn, turn_count):
        return OpenLoop(
            loop_id=f"l-{turn_count}",
            salience=1.0,
            last_touched_turn=turn_count,
            summary="open thread",
        )


class _StubPressure:
    def __init__(self, value: float = 0.4):
        self.value = value

    def compute(self, turn, topical_vector, turn_count):
        return self.value


# ---- Tests ----------------------------------------------------------------


def test_initial_state_defaults():
    f = FringeState()
    assert f.embedding_dim == 384
    assert f.topical_vector.shape == (384,)
    assert np.allclose(f.topical_vector, 0.0)
    assert f.affective_tilt.shape == (6,)
    assert f.register.shape == (3,)
    assert pytest.approx(float(f.register.sum()), abs=1e-6) == 1.0
    assert f.open_loops == []
    assert f.turn_count == 0
    assert f.temporal_pressure == 0.0


def test_update_increments_turn_count():
    f = FringeState()
    deps = dict(
        embedder=_StubEmbedder(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),
        loop_tracker=_StubLoopNone(),
        pressure_computer=_StubPressure(0.5),
    )
    f.update(Turn(user="a", assistant="b"), **deps)
    assert f.turn_count == 1
    f.update(Turn(user="c", assistant="d"), **deps)
    assert f.turn_count == 2


def test_update_moves_register_toward_detected():
    f = FringeState()
    deps = dict(
        embedder=_StubEmbedder(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),  # tilts technical
        loop_tracker=_StubLoopNone(),
        pressure_computer=_StubPressure(0.0),
    )
    # After many updates, technical should dominate.
    for _ in range(60):
        f.update(Turn(user="x", assistant="y"), **deps)
    assert f._dominant_register() == "technical"
    assert pytest.approx(float(f.register.sum()), abs=1e-6) == 1.0


def test_update_topical_drift_ema():
    f = FringeState()
    deps = dict(
        embedder=_StubEmbedder(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),
        loop_tracker=_StubLoopNone(),
        pressure_computer=_StubPressure(0.0),
    )
    f.update(Turn(user="alpha", assistant="beta"), **deps)
    norm_after_one = float(np.linalg.norm(f.topical_vector))
    assert norm_after_one > 0.0
    # Topical EMA: after one update, magnitude should be ≤ (1 - decay) of
    # a unit vector, i.e. ≤ 0.15 + small slack.
    assert norm_after_one <= 0.16 + 1e-6


def test_loops_decay_and_evict():
    f = FringeState()
    deps = dict(
        embedder=_StubEmbedder(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),
        loop_tracker=_StubLoopAlways(),
        pressure_computer=_StubPressure(0.0),
    )
    # First update adds one. Subsequent updates: existing decays, new added.
    f.update(Turn(user="a", assistant="b"), **deps)
    assert len(f.open_loops) == 1
    # After enough updates with LOOP_DECAY=0.9 and threshold 0.1, old loops
    # eventually get evicted. But _StubLoopAlways adds a new one each turn.
    # Run many turns and confirm the count caps somewhere reasonable.
    for _ in range(50):
        f.update(Turn(user="a", assistant="b"), **deps)
    # With salience threshold 0.1 and decay 0.9, a loop survives ~22 turns.
    # So at steady state we expect ~20-25 loops, not unbounded growth.
    assert 10 <= len(f.open_loops) <= 30


def test_temporal_pressure_clamped():
    f = FringeState()
    deps = dict(
        embedder=_StubEmbedder(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),
        loop_tracker=_StubLoopNone(),
        pressure_computer=_StubPressure(5.0),  # would-be out of range
    )
    f.update(Turn(user="a", assistant="b"), **deps)
    assert -1.0 <= f.temporal_pressure <= 1.0


def test_reset_clears_everything():
    f = FringeState()
    deps = dict(
        embedder=_StubEmbedder(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),
        loop_tracker=_StubLoopAlways(),
        pressure_computer=_StubPressure(0.7),
    )
    for _ in range(5):
        f.update(Turn(user="a", assistant="b"), **deps)
    assert f.turn_count == 5
    f.reset()
    assert f.turn_count == 0
    assert f.temporal_pressure == 0.0
    assert f.open_loops == []
    assert np.allclose(f.topical_vector, 0.0)


def test_serialization_roundtrip():
    f = FringeState()
    deps = dict(
        embedder=_StubEmbedder(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),
        loop_tracker=_StubLoopAlways(),
        pressure_computer=_StubPressure(0.4),
    )
    for _ in range(3):
        f.update(Turn(user="hello", assistant="hi"), **deps)
    data = f.to_dict()
    restored = FringeState.from_dict(data)
    assert restored.turn_count == f.turn_count
    assert restored.embedding_dim == f.embedding_dim
    assert np.allclose(restored.topical_vector, f.topical_vector, atol=1e-5)
    assert np.allclose(restored.register, f.register, atol=1e-5)
    assert restored.temporal_pressure == pytest.approx(f.temporal_pressure)
    assert len(restored.open_loops) == len(f.open_loops)


def test_decay_to_now_attenuates_after_hours():
    f = FringeState(embedding_dim=8)
    f.topical_vector = np.ones(8, dtype=np.float32)
    f.affective_tilt = np.ones(6, dtype=np.float32) * 0.5
    f.temporal_pressure = 0.8
    f.open_loops = [OpenLoop("l1", 1.0, 1, "thread")]
    # Pretend last_updated was a long time ago.
    f.last_updated = datetime.now() - timedelta(hours=20)
    f.decay_to_now()
    # decay_factor = 0.95**20 ≈ 0.358
    assert float(f.topical_vector[0]) < 0.5
    assert f.temporal_pressure < 0.4
    # 1.0 * 0.358 = 0.358 still > 0.1 threshold
    assert len(f.open_loops) == 1
    # Register stays at uniform (we never set it).
    assert pytest.approx(float(f.register.sum()), abs=1e-6) == 1.0


def test_decay_to_now_no_op_for_recent_writes():
    f = FringeState(embedding_dim=8)
    f.topical_vector = np.ones(8, dtype=np.float32)
    before = f.topical_vector.copy()
    # last_updated is now (default)
    f.decay_to_now()
    assert np.allclose(f.topical_vector, before)


def test_to_prompt_prefix_format():
    f = FringeState()
    prefix = f.to_prompt_prefix()
    assert prefix.startswith("[Conversational fringe:")
    assert prefix.endswith(".]")
    assert "register tilting" in prefix
    assert "affect" in prefix
    assert "pace" in prefix


def test_to_prompt_prefix_with_pressure_labels():
    f = FringeState()
    f.temporal_pressure = 0.6
    assert "building" in f.to_prompt_prefix()
    f.temporal_pressure = -0.6
    assert "wandering" in f.to_prompt_prefix()
    f.temporal_pressure = 0.0
    assert "steady" in f.to_prompt_prefix()


def test_to_retrieval_bias_returns_copy():
    f = FringeState(embedding_dim=4)
    f.topical_vector = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    bias = f.to_retrieval_bias()
    assert np.allclose(bias, f.topical_vector)
    bias[0] = 99.0
    # Mutating the returned bias must not affect the fringe.
    assert float(f.topical_vector[0]) == pytest.approx(0.1)


def test_update_does_not_raise_on_failure():
    """Fringe failure must not break the turn — exceptions inside update()
    should be swallowed."""
    class Boom:
        def embed(self, text):
            raise RuntimeError("kaboom")
    f = FringeState()
    f.update(
        turn=Turn(user="x", assistant="y"),
        embedder=Boom(),
        affect_scorer=_StubAffect(),
        register_detector=_StubRegister(),
        loop_tracker=_StubLoopNone(),
        pressure_computer=_StubPressure(0.0),
    )
    # turn_count still incremented (we did start the update); state otherwise
    # left in a sane zone.
    assert f.turn_count == 1
