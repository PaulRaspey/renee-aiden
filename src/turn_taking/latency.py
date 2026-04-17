"""
Variable response latency controller (M8).

Humans don't respond with constant latency. Fast to acknowledgments, slow
to emotional content, slowest to hard truths. A 200ms response to "my dog
died last night" is sociopathic — this module says so with numbers.

Base latencies come from architecture/05_turn_taking.md; mood modulates
them (tired slower, playful faster, scattered slower). A natural variance
jitter is applied on every call so the latency distribution has spread.

When the target latency is long (>600ms), the controller flags that a
subtle thinking paralinguistic should play at the midpoint, per the
architecture's guidance.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class TurnType(str, Enum):
    ACKNOWLEDGMENT = "acknowledgment"
    SIMPLE_QUESTION = "simple_question"
    NORMAL_RESPONSE = "normal_response"
    THOUGHTFUL_RESPONSE = "thoughtful_response"
    EMOTIONAL_RESPONSE = "emotional_response"
    DIFFICULT_TRUTH = "difficult_truth"


BASE_LATENCY_MS: dict[TurnType, int] = {
    TurnType.ACKNOWLEDGMENT: 150,
    TurnType.SIMPLE_QUESTION: 300,
    TurnType.NORMAL_RESPONSE: 500,
    TurnType.THOUGHTFUL_RESPONSE: 900,
    TurnType.EMOTIONAL_RESPONSE: 1200,
    TurnType.DIFFICULT_TRUTH: 1500,
}


_ACK_HEAD = re.compile(
    r"^\s*(yeah|yes|yep|right|mhm|mm|sure|ok(ay)?|no|nope|nah|"
    r"of course|got it|exactly|totally|cool|great|thanks?)\b",
    re.I,
)
_QUESTION_TAIL = re.compile(r"\?\s*$")


@dataclass
class LatencyPlan:
    turn_type: TurnType
    target_ms: int
    include_thinking_filler: bool
    reason: str = ""


def classify_turn(
    user_text: str,
    *,
    is_vulnerable_admission: bool = False,
    is_difficult_truth: bool = False,
    is_emotional: bool = False,
    is_thoughtful: bool = False,
) -> TurnType:
    """
    Pick the TurnType. Context flags override the text-shape heuristic so an
    upstream classifier can always force the right bucket.
    """
    if is_difficult_truth:
        return TurnType.DIFFICULT_TRUTH
    if is_vulnerable_admission or is_emotional:
        return TurnType.EMOTIONAL_RESPONSE
    if is_thoughtful:
        return TurnType.THOUGHTFUL_RESPONSE

    text = (user_text or "").strip()
    words = text.split()
    if not words:
        return TurnType.NORMAL_RESPONSE
    if _ACK_HEAD.match(text) and len(words) <= 4:
        return TurnType.ACKNOWLEDGMENT
    if _QUESTION_TAIL.search(text) and len(words) <= 10:
        return TurnType.SIMPLE_QUESTION
    if len(words) > 40:
        return TurnType.THOUGHTFUL_RESPONSE
    return TurnType.NORMAL_RESPONSE


def target_latency_ms(
    turn_type: TurnType,
    mood: Any = None,
    *,
    rng: Optional[random.Random] = None,
    variance_sigma: float = 0.12,
) -> int:
    """
    Return the target wait (ms) from user-stops to Renée-first-audio.
    Mood modulates base: tired slower, playful faster, scattered slower.
    """
    from ..voice.prosody import MoodLike

    m = MoodLike.from_obj(mood) if mood is not None else MoodLike()
    base: float = float(BASE_LATENCY_MS[turn_type])

    if m.energy < 0.4:
        base *= 1.20
    if m.playfulness > 0.7:
        base *= 0.85
    if m.focus < 0.4:
        base *= 1.15
    if m.patience < 0.35 and turn_type != TurnType.DIFFICULT_TRUTH:
        # low patience = snaps back faster (except on hard truths)
        base *= 0.9

    rng = rng or random.Random()
    jitter = rng.gauss(1.0, variance_sigma)
    jitter = max(0.75, min(1.30, jitter))
    result = int(base * jitter)
    return max(80, result)


def plan_latency(
    user_text: str,
    mood: Any = None,
    *,
    context_flags: Optional[dict] = None,
    rng: Optional[random.Random] = None,
    thinking_filler_threshold_ms: int = 600,
) -> LatencyPlan:
    flags = context_flags or {}
    tt = classify_turn(
        user_text,
        is_vulnerable_admission=bool(flags.get("is_vulnerable_admission")),
        is_difficult_truth=bool(flags.get("is_difficult_truth")),
        is_emotional=bool(flags.get("is_emotional")),
        is_thoughtful=bool(flags.get("is_thoughtful")),
    )
    target = target_latency_ms(tt, mood, rng=rng)
    include_filler = target >= thinking_filler_threshold_ms
    return LatencyPlan(
        turn_type=tt,
        target_ms=target,
        include_thinking_filler=include_filler,
        reason=f"{tt.value} / mood-adjusted",
    )
