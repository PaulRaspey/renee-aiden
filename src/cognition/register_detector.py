"""
RegisterDetector — heuristic v1. Returns 3-simplex over
[technical, intimate, playful], i.e. always sums to 1.

Both sides scored, weighted (0.4 user / 0.6 assistant), then projected to
the simplex via softmax (so a uniformly-flat input still produces 1/3 each
rather than NaN).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .fringe import Turn

DIMS = ("technical", "intimate", "playful")

_TECH_TOKENS = (
    "function", "variable", "config", "endpoint", "schema", "vector",
    "embedding", "kernel", "thread", "buffer", "regex", "ssh", "api",
    "json", "sql", "db", "tcp", "http", "auth", "token", "hash",
    "deploy", "build", "test", "lint", "compile",
)
_INTIMATE_TOKENS = (
    "feel", "felt", "feeling", "love", "miss", "scared", "lonely",
    "happy", "sad", "tender", "vulnerable", "honest", "honestly",
    "trust", "afraid",
)
_PLAYFUL_TOKENS = (
    "lol", "haha", "lmao", "kidding", "joking", "ridiculous", "absurd",
    "imagine", "what if", "gotcha",
)


def _word_count(text: str) -> int:
    return max(1, len(text.split()))


def _count_hits(text: str, needles: tuple[str, ...]) -> int:
    return sum(text.count(n) for n in needles)


def _has_code_block(text: str) -> bool:
    return "```" in text or bool(re.search(r"`[^`]+`", text))


def _has_numeric(text: str) -> bool:
    return bool(re.search(r"\d", text))


def _exclam_density(text: str) -> float:
    return text.count("!") / _word_count(text)


def _first_person_singular(text: str) -> int:
    return len(re.findall(r"\bi\b|\bme\b|\bmy\b|\bmine\b", text))


def _second_person(text: str) -> int:
    return len(re.findall(r"\byou\b|\byour\b|\byours\b", text))


def _score_text(text: str) -> np.ndarray:
    """Score one side. Returns 3 raw scores (not normalized)."""
    if not text:
        return np.zeros(3, dtype=np.float32)
    lower = text.lower()
    wc = _word_count(lower)

    tech = (
        _count_hits(lower, _TECH_TOKENS) / wc * 5
        + (1.0 if _has_code_block(text) else 0.0)
        + (0.3 if _has_numeric(text) else 0.0)
    )

    intimate = (
        _count_hits(lower, _INTIMATE_TOKENS) / wc * 6
        + _first_person_singular(lower) / wc * 2
        + _second_person(lower) / wc * 1.5
    )

    playful = (
        _count_hits(lower, _PLAYFUL_TOKENS) / wc * 6
        + _exclam_density(lower) * 4
    )

    return np.array([tech, intimate, playful], dtype=np.float32)


def _to_simplex(scores: np.ndarray) -> np.ndarray:
    """Project to 3-simplex via softmax. A flat zero input yields uniform 1/3."""
    # Stable softmax
    m = float(np.max(scores))
    exps = np.exp(scores - m)
    total = float(exps.sum())
    if total <= 0:
        return np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32)
    return (exps / total).astype(np.float32)


@dataclass
class RegisterDetector:
    """Heuristic register detector v1."""

    user_weight: float = 0.4
    assistant_weight: float = 0.6

    def detect(self, turn: Turn) -> np.ndarray:
        u = _score_text(turn.user)
        a = _score_text(turn.assistant)
        combined = self.user_weight * u + self.assistant_weight * a
        return _to_simplex(combined)
