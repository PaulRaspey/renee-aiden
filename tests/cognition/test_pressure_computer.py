"""Unit tests for PressureComputer."""
from __future__ import annotations

import numpy as np
import pytest

from src.cognition.fringe import Turn
from src.cognition.pressure_computer import PressureComputer


class _AlignedEmbedder:
    """Returns vectors that align with a target direction."""
    def __init__(self, dim: int = 8, alignment: float = 0.95):
        self.dim = dim
        self.alignment = alignment
        self._target = np.ones(dim, dtype=np.float32) / np.sqrt(dim)

    def embed(self, text: str) -> np.ndarray:
        # Convex combo of target and noise.
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        noise = rng.standard_normal(self.dim).astype(np.float32)
        noise = noise / (np.linalg.norm(noise) + 1e-9)
        v = self.alignment * self._target + (1 - self.alignment) * noise
        return v / (np.linalg.norm(v) + 1e-9)

    def target(self) -> np.ndarray:
        return self._target.copy()


class _OrthogonalEmbedder:
    """Returns vectors orthogonal to a target direction."""
    def __init__(self, dim: int = 8):
        self.dim = dim
        self._target = np.ones(dim, dtype=np.float32) / np.sqrt(dim)

    def embed(self, text: str) -> np.ndarray:
        # Vector orthogonal-ish to the target: alternating signs.
        v = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(self.dim)], dtype=np.float32)
        v = v - np.dot(v, self._target) * self._target
        v = v / (np.linalg.norm(v) + 1e-9)
        return v


def test_zero_topical_vector_yields_zero_pressure():
    pc = PressureComputer(embedder=_AlignedEmbedder())
    out = pc.compute(
        turn=Turn(user="a", assistant="b"),
        topical_vector=np.zeros(8, dtype=np.float32),
        turn_count=1,
    )
    assert out == 0.0


def test_aligned_embedding_yields_positive_pressure():
    emb = _AlignedEmbedder(alignment=0.99)
    pc = PressureComputer(embedder=emb)
    target = emb.target()
    # Run several turns to seed the rolling window
    for i in range(5):
        out = pc.compute(
            turn=Turn(user=f"user{i}", assistant=f"assistant{i}"),
            topical_vector=target,
            turn_count=i + 1,
        )
    assert out > 0.3, f"expected building pressure, got {out}"


def test_orthogonal_embedding_yields_negative_pressure():
    pc = PressureComputer(embedder=_OrthogonalEmbedder())
    target = np.ones(8, dtype=np.float32) / np.sqrt(8)
    for i in range(5):
        out = pc.compute(
            turn=Turn(user=f"u{i}", assistant=f"a{i}"),
            topical_vector=target,
            turn_count=i + 1,
        )
    assert out < -0.3, f"expected wandering pressure, got {out}"


def test_output_bounded():
    pc = PressureComputer(embedder=_AlignedEmbedder(alignment=1.0))
    target = np.ones(8, dtype=np.float32) / np.sqrt(8)
    out = pc.compute(
        turn=Turn(user="x", assistant="y"),
        topical_vector=target,
        turn_count=1,
    )
    assert -1.0 <= out <= 1.0


def test_no_embedder_yields_zero():
    pc = PressureComputer(embedder=None)
    out = pc.compute(
        turn=Turn(user="x", assistant="y"),
        topical_vector=np.ones(8, dtype=np.float32),
        turn_count=1,
    )
    assert out == 0.0


def test_embedder_failure_swallowed():
    class Broken:
        def embed(self, text):
            raise RuntimeError("nope")
    pc = PressureComputer(embedder=Broken())
    out = pc.compute(
        turn=Turn(user="x", assistant="y"),
        topical_vector=np.ones(8, dtype=np.float32),
        turn_count=1,
    )
    assert out == 0.0


def test_reset_clears_window():
    pc = PressureComputer(embedder=_AlignedEmbedder())
    target = np.ones(8, dtype=np.float32) / np.sqrt(8)
    for i in range(3):
        pc.compute(Turn(user="a", assistant="b"), target, i + 1)
    assert len(pc._history) == 3
    pc.reset()
    assert len(pc._history) == 0
