"""
LoopTracker — heuristic v1.

Detects when a turn raises a question that won't be resolved this turn.
Triggers:
  - Explicit question without immediate self-answer.
  - "let me think about", "I'll come back to", "one more thing", etc.
  - Topic shift markers that leave a previous topic mid-sentence.

Returns a new OpenLoop with salience=1.0, or None.

Scans the assistant side first (Renée's deliberate phrasing); falls back to
user side. Either side raising a loop counts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .fringe import OpenLoop, Turn

_DEFER_MARKERS = (
    "let me think about",
    "i'll come back to",
    "i will come back to",
    "i'll get back to",
    "remind me to",
    "we should circle back",
    "circle back to",
    "one more thing",
    "actually wait",
    "hold that thought",
    "we'll get to",
    "i need to think",
)

_SHIFT_MARKERS = (
    "but anyway",
    "but actually",
    "anyway,",
    "different topic",
    "changing subjects",
    "side note",
    "tangent,",
    "by the way",
)


def _ends_with_question(text: str) -> bool:
    stripped = (text or "").strip()
    return stripped.endswith("?")


def _has_self_answer(text: str) -> bool:
    """Heuristic: a question followed by ≥10 words of declarative content
    counts as self-answered."""
    text = text or ""
    if "?" not in text:
        return False
    after = text.split("?", 1)[1].strip()
    if not after:
        return False
    # bail if the post-question content is itself another question
    if after.endswith("?"):
        return False
    return len(after.split()) >= 10


def _contains_marker(text: str, markers: tuple[str, ...]) -> Optional[str]:
    lower = (text or "").lower()
    for m in markers:
        if m in lower:
            return m
    return None


def _summarize(text: str, max_words: int = 6) -> str:
    """Cheap summary: first N words of the trigger text, lowercased."""
    words = (text or "").strip().split()
    if not words:
        return "open thread"
    return " ".join(words[:max_words]).lower().rstrip(".,!?")


@dataclass
class LoopTracker:
    """Heuristic loop tracker v1."""

    def check(self, turn: Turn, turn_count: int) -> Optional[OpenLoop]:
        # Prefer the assistant side as the trigger source for summarization.
        for source_label, text in (("assistant", turn.assistant), ("user", turn.user)):
            if not text:
                continue

            marker = _contains_marker(text, _DEFER_MARKERS)
            if marker:
                return OpenLoop(
                    loop_id=f"loop-{turn.turn_id}-{source_label}",
                    salience=1.0,
                    last_touched_turn=turn_count,
                    summary=_summarize(text),
                )

            shift = _contains_marker(text, _SHIFT_MARKERS)
            if shift:
                # Topic shift means the *previous* topic became an open loop.
                # We don't have access to it here, so summarize the shift itself.
                return OpenLoop(
                    loop_id=f"loop-{turn.turn_id}-{source_label}-shift",
                    salience=0.7,  # weaker than an explicit defer
                    last_touched_turn=turn_count,
                    summary=f"shifted from: {_summarize(text)}",
                )

            # Trailing unanswered question.
            if _ends_with_question(text) and not _has_self_answer(text):
                # Pull just the question for the summary.
                q_text = text.rsplit("?", 1)[0].rsplit(".", 1)[-1].strip() or text.strip()
                return OpenLoop(
                    loop_id=f"loop-{turn.turn_id}-{source_label}-q",
                    salience=0.8,
                    last_touched_turn=turn_count,
                    summary=_summarize(q_text + "?"),
                )

        return None
