"""
PressureComputer — heuristic v1.

Returns a signed scalar in [-1, 1]:
  +pressure = building (current turn aligns with the established trajectory)
  -pressure = wandering (current turn diverges)

Implementation: cosine similarity between an embedder-produced vector for
the joint turn text and the current topical_vector. The topical_vector is
already an EMA over recent turns, so a single similarity check effectively
captures alignment-with-trajectory.

A short rolling window of recent similarities is held inside the computer
to smooth turn-to-turn noise. The window is per-instance — if you want
per-persona pressure, instantiate one PressureComputer per persona (which
is what PersonaCore does).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .fringe import Turn


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-9 or nb <= 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


@dataclass
class PressureComputer:
    """Stateful heuristic — keeps a small rolling window of recent sims."""

    embedder: Optional[object] = None  # embedder with .embed(text) -> np.ndarray
    window: int = 5
    _history: deque = field(default_factory=lambda: deque(maxlen=5))

    def __post_init__(self):
        # Resize the deque to match window if user passed a non-default value.
        if self._history.maxlen != self.window:
            self._history = deque(self._history, maxlen=self.window)

    def compute(self, turn: Turn, topical_vector: np.ndarray, turn_count: int) -> float:
        if self.embedder is None or topical_vector is None:
            return 0.0
        # If topical_vector has no signal yet (very early in the convo), no
        # meaningful pressure can be inferred.
        if float(np.linalg.norm(topical_vector)) < 1e-6:
            return 0.0
        joint = f"{turn.user}\n{turn.assistant}".strip() or " "
        try:
            emb = np.asarray(self.embedder.embed(joint), dtype=np.float32)
        except Exception:
            return 0.0
        sim = _cosine(emb, topical_vector)
        # Map cosine [-1, 1] to pressure [-1, 1] directly. Track in window
        # for smoothing.
        self._history.append(sim)
        smoothed = sum(self._history) / len(self._history)
        # Center: a cosine of 0.5 (decent topical match) → roughly neutral
        # pressure; >0.7 → building; <0.3 → wandering. Affine remap so the
        # neutral point is where it should be.
        pressure = (smoothed - 0.5) * 2.0
        return max(-1.0, min(1.0, pressure))

    def reset(self) -> None:
        self._history.clear()
