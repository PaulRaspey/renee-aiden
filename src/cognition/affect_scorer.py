"""
AffectScorer — heuristic v1. Returns 6-dim vector over
[sharpening, softening, opening, closing, warming, cooling].

Each dim is scored independently in [0, 1] from text features. Both sides
of the turn are scored; assistant weighted higher (0.6) than user (0.4) on
the assumption that the assistant's register choices are more deliberate
than user transcription.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .fringe import Turn

DIMS = ("sharpening", "softening", "opening", "closing", "warming", "cooling")

_HEDGES = (
    "maybe", "i think", "i guess", "kind of", "sort of", "perhaps",
    "i suppose", "possibly", "might be", "could be", "i wonder",
)
_APOLOGIES = ("sorry", "apolog", "my bad", "i'm sorry", "didn't mean")
_TECH_TOKENS = (
    "function", "variable", "config", "endpoint", "schema", "vector",
    "embedding", "kernel", "thread", "buffer", "regex", "ssh", "api",
    "json", "sql", "db", "tcp", "http", "auth", "token", "hash",
)
_OPENERS = (
    "what if", "imagine", "what about", "could we", "what could",
    "have you ever", "ever wonder", "tell me about",
)
_CLOSERS = (
    "anyway", "moving on", "alright then", "ok so", "in any case",
    "let's leave it", "either way",
)
_WARM_TOKENS = (
    "we", "us", "our", "love", "miss", "happy", "warm", "close",
    "together", "honest", "honestly", "really feel",
)
_COOL_TOKENS = (
    "however", "moreover", "therefore", "furthermore", "additionally",
    "nevertheless", "respectively", "accordingly", "in conclusion",
)


def _normalize(s: str) -> str:
    return (s or "").lower()


def _count_hits(text: str, needles: tuple[str, ...]) -> int:
    return sum(text.count(n) for n in needles)


def _word_count(text: str) -> int:
    return max(1, len(text.split()))


def _question_count(text: str) -> int:
    return text.count("?")


def _exclam_count(text: str) -> int:
    return text.count("!")


def _emoji_count(text: str) -> int:
    # crude: anything outside Basic Latin + Latin-1 Supplement.
    return sum(1 for c in text if ord(c) > 0x2700)


def _first_person_singular(text: str) -> int:
    return len(re.findall(r"\bi\b|\bme\b|\bmy\b|\bmine\b", text))


def _first_person_plural(text: str) -> int:
    return len(re.findall(r"\bwe\b|\bus\b|\bour\b|\bours\b", text))


def _third_person(text: str) -> int:
    return len(re.findall(r"\bthey\b|\bthem\b|\btheir\b|\bone\b", text))


def _has_code_block(text: str) -> bool:
    return "```" in text or bool(re.search(r"`[^`]+`", text))


def _score_text(text: str) -> np.ndarray:
    """Score one side. Returns 6-dim float array in [0, 1]."""
    if not text:
        return np.zeros(6, dtype=np.float32)
    lower = _normalize(text)
    wc = _word_count(lower)

    # sharpening: question specificity, declarative density, technical terms
    tech_rate = _count_hits(lower, _TECH_TOKENS) / wc
    code = 1.0 if _has_code_block(text) else 0.0
    declarative = max(0.0, 1.0 - (_question_count(lower) + _exclam_count(lower)) / wc * 4)
    sharpening = min(1.0, tech_rate * 4 + code * 0.5 + declarative * 0.2)

    # softening: hedges, apologies, emoji
    hedges = _count_hits(lower, _HEDGES) / wc
    apologies = _count_hits(lower, _APOLOGIES) / wc
    emoji = _emoji_count(text) / wc
    softening = min(1.0, hedges * 6 + apologies * 4 + emoji * 4)

    # opening: open-ended question markers
    openers = _count_hits(lower, _OPENERS)
    open_q = _question_count(lower)
    opening = min(1.0, openers * 0.4 + open_q / max(1, wc) * 6)

    # closing: definitive closers, period density
    closers = _count_hits(lower, _CLOSERS)
    period_density = lower.count(".") / wc
    closing = min(1.0, closers * 0.5 + period_density * 0.5)

    # warming: first-person plural, emotional vocabulary
    fpp = _first_person_plural(lower)
    warm_tokens = _count_hits(lower, _WARM_TOKENS)
    warming = min(1.0, fpp / wc * 6 + warm_tokens / wc * 4)

    # cooling: formality / abstract / third-person
    cool_tokens = _count_hits(lower, _COOL_TOKENS)
    third = _third_person(lower)
    cooling = min(1.0, cool_tokens / wc * 6 + third / wc * 3)

    return np.array(
        [sharpening, softening, opening, closing, warming, cooling],
        dtype=np.float32,
    )


@dataclass
class AffectScorer:
    """Heuristic affect scorer v1."""

    user_weight: float = 0.4
    assistant_weight: float = 0.6

    def score(self, turn: Turn) -> np.ndarray:
        u = _score_text(turn.user)
        a = _score_text(turn.assistant)
        combined = self.user_weight * u + self.assistant_weight * a
        return combined.astype(np.float32)
