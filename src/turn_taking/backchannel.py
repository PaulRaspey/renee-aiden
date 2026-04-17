"""
Backchannel layer (M9).

Runs parallel to user speech. Watches the ASR partial transcript and
simple acoustic hints (silence duration, rising intonation, energy) and
decides when to fire a short paralinguistic token — "mhm", "yeah", "mm",
"right" — to be mixed under user audio at -6dB.

Hard rules (architecture/04_paralinguistics.md + 05_turn_taking.md):
  - Never backchannel during factual disagreement.
  - Never backchannel during user distress.
  - Never backchannel during a heated conversation tone.
  - Never backchannel while Renée is speaking (caller's responsibility to
    set `context.user_speaking=False` in that state).

Rate scales with `mood.warmth` and `context.intimacy`.

This module is deterministic given its RNG seed, so the eval harness
can replay a session and get the same backchannel trace.
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..paralinguistics.injector import ClipLibrary, MoodLike


class BackchannelTrigger(str, Enum):
    CLAUSE_BOUNDARY = "clause_boundary"
    RISING_INTONATION = "rising_intonation"
    CONFIRMATION_SEEKING = "confirmation_seeking"
    EMOTIONAL_CONTENT = "emotional_content"
    INTIMATE_MOMENT = "intimate_moment"


# Words in the user transcript that suggest emotional content worth acknowledging.
_EMOTION_MARKERS = frozenset({
    "died", "dying", "death", "lost", "loss", "broke", "broken",
    "scared", "afraid", "nervous", "worried", "anxious",
    "miss", "missing", "missed", "alone", "lonely",
    "hurt", "hurts", "hurting", "tired", "exhausted",
    "sick", "upset", "angry", "furious",
})

# Connective endings that signal the clause isn't finished.
_CLAUSE_CONNECTIVES = (" so", " and", " but", " because", " or", " then")


@dataclass
class BackchannelContext:
    user_speaking: bool = True
    is_disagreement: bool = False
    user_distressed: bool = False
    conversation_tone: str = "casual"   # casual|playful|serious|vulnerable|heated
    intimacy: float = 0.4               # 0..1
    mood: Optional[MoodLike] = None


@dataclass
class BackchannelToken:
    category: str
    subcategory: str
    intensity: float
    volume_db: float = -6.0
    clip_path: Optional[Path] = None
    trigger: str = ""


@dataclass
class BackchannelEvent:
    token: BackchannelToken
    at_ms: float
    transcript_at_fire: str
    p_fire: float = 0.0
    reason: str = ""


class BackchannelLayer:
    def __init__(
        self,
        library: Optional[ClipLibrary] = None,
        *,
        min_gap_ms: int = 1800,
        max_per_minute: int = 8,
        base_probability: float = 0.35,
        rng: Optional[random.Random] = None,
    ):
        self.library = library
        self.min_gap_ms = min_gap_ms
        self.max_per_minute = max_per_minute
        self.base_probability = base_probability
        self.rng = rng or random.Random()
        self._last_fire_ms: float = float("-inf")
        self._history: deque[tuple[str, float]] = deque()

    # ------------------------------------------------------------------
    # detection
    # ------------------------------------------------------------------

    def detect_opportunity(
        self,
        transcript: str,
        *,
        silence_ms: int,
        rising_intonation: bool = False,
        energy_low: bool = False,
    ) -> Optional[BackchannelTrigger]:
        text = transcript.strip()
        if not text:
            return None

        lowered = text.lower()
        tail_lower = lowered[-10:]

        # Question-like end -> user seeking confirmation.
        if text.endswith("?"):
            return BackchannelTrigger.CONFIRMATION_SEEKING
        if rising_intonation:
            return BackchannelTrigger.RISING_INTONATION

        # Mid-clause tiny pause + comma/semicolon/dash -> clause boundary.
        if 120 <= silence_ms < 500 and text.endswith((",", ";", "—", "–")):
            return BackchannelTrigger.CLAUSE_BOUNDARY
        # Mid-clause tiny pause + connective last word.
        if 150 <= silence_ms < 500:
            for conn in _CLAUSE_CONNECTIVES:
                if tail_lower.endswith(conn):
                    return BackchannelTrigger.CLAUSE_BOUNDARY

        # Emotional content in what the user said.
        words = set(_tokenize(lowered))
        if words & _EMOTION_MARKERS and silence_ms >= 80:
            return BackchannelTrigger.EMOTIONAL_CONTENT

        # Intimate quiet moment.
        if energy_low and silence_ms >= 300:
            return BackchannelTrigger.INTIMATE_MOMENT

        return None

    # ------------------------------------------------------------------
    # scoring
    # ------------------------------------------------------------------

    def should_fire(
        self,
        trigger: Optional[BackchannelTrigger],
        context: BackchannelContext,
        *,
        now_ms: float,
    ) -> float:
        if trigger is None:
            return 0.0
        if not context.user_speaking:
            return 0.0

        # HARD RULES — never backchannel into these states.
        if context.is_disagreement:
            return 0.0
        if context.user_distressed:
            return 0.0
        if context.conversation_tone == "heated":
            return 0.0

        # Rate caps.
        if now_ms - self._last_fire_ms < self.min_gap_ms:
            return 0.0
        cutoff = now_ms - 60_000
        while self._history and self._history[0][1] < cutoff:
            self._history.popleft()
        if len(self._history) >= self.max_per_minute:
            return 0.0

        p = float(self.base_probability)
        m = context.mood or MoodLike()

        # Warmth scales frequency up; ranges ~0.5..1.3 around a mid point.
        p *= 0.5 + 0.8 * m.warmth
        # Intimacy adds a small additional multiplier 0.8..1.2.
        p *= 0.8 + 0.4 * max(0.0, min(1.0, context.intimacy))

        # Conversation tone multipliers.
        tone_mult = {
            "casual": 1.0,
            "playful": 1.10,
            "serious": 0.75,
            "vulnerable": 0.7,
        }.get(context.conversation_tone, 1.0)
        p *= tone_mult

        # Trigger type shapes the fire probability.
        trigger_mult = {
            BackchannelTrigger.CLAUSE_BOUNDARY: 1.0,
            BackchannelTrigger.CONFIRMATION_SEEKING: 1.25,
            BackchannelTrigger.RISING_INTONATION: 1.20,
            BackchannelTrigger.EMOTIONAL_CONTENT: 0.90,
            BackchannelTrigger.INTIMATE_MOMENT: 0.80,
        }.get(trigger, 1.0)
        p *= trigger_mult

        return max(0.0, min(1.0, p))

    # ------------------------------------------------------------------
    # token selection
    # ------------------------------------------------------------------

    def pick_token(
        self,
        trigger: BackchannelTrigger,
        context: BackchannelContext,
    ) -> Optional[BackchannelToken]:
        m = context.mood or MoodLike()

        if trigger in (BackchannelTrigger.EMOTIONAL_CONTENT, BackchannelTrigger.INTIMATE_MOMENT):
            options = [("affirmations", "mhm"), ("thinking", "mm")]
        elif trigger == BackchannelTrigger.CONFIRMATION_SEEKING:
            options = [("affirmations", "yeah"), ("affirmations", "right"), ("affirmations", "mhm")]
        elif trigger == BackchannelTrigger.RISING_INTONATION:
            options = [("affirmations", "yeah"), ("affirmations", "mhm")]
        else:  # CLAUSE_BOUNDARY
            options = [("affirmations", "mhm"), ("thinking", "mm"), ("affirmations", "right")]

        weights: list[float] = []
        for cat, sub in options:
            w = 1.0
            if m.warmth > 0.8 and sub in ("mhm", "yeah"):
                w += 1.0
            if m.warmth <= 0.6 and sub == "mm":
                w += 0.6
            if trigger == BackchannelTrigger.EMOTIONAL_CONTENT and sub == "mhm":
                w += 0.5
            weights.append(w)

        total = sum(weights)
        pick = self.rng.uniform(0, total)
        cum = 0.0
        cat, sub = options[-1]
        for (c, s), w in zip(options, weights):
            cum += w
            if pick <= cum:
                cat, sub = c, s
                break

        intensity = round(0.25 + 0.30 * m.warmth, 3)
        token = BackchannelToken(
            category=cat,
            subcategory=sub,
            intensity=intensity,
            volume_db=-6.0,
            trigger=trigger.value,
        )

        if self.library is not None:
            clips = self.library.get(cat, sub)
            if clips:
                clip = self.rng.choice(clips)
                abs_path = clip.get("_abs")
                if abs_path is not None:
                    token.clip_path = Path(str(abs_path))
        return token

    # ------------------------------------------------------------------
    # event-level API
    # ------------------------------------------------------------------

    def observe(
        self,
        transcript: str,
        *,
        silence_ms: int,
        context: BackchannelContext,
        rising_intonation: bool = False,
        energy_low: bool = False,
        now_ms: Optional[float] = None,
    ) -> Optional[BackchannelEvent]:
        now = now_ms if now_ms is not None else time.time() * 1000.0
        trigger = self.detect_opportunity(
            transcript,
            silence_ms=silence_ms,
            rising_intonation=rising_intonation,
            energy_low=energy_low,
        )
        if trigger is None:
            return None
        p = self.should_fire(trigger, context, now_ms=now)
        if p <= 0.0:
            return None
        if self.rng.random() > p:
            return None
        token = self.pick_token(trigger, context)
        if token is None:
            return None
        self._last_fire_ms = now
        self._history.append((f"{token.category}/{token.subcategory}", now))
        return BackchannelEvent(
            token=token,
            at_ms=now,
            transcript_at_fire=transcript,
            p_fire=p,
            reason=trigger.value,
        )

    def reset(self) -> None:
        self._last_fire_ms = float("-inf")
        self._history.clear()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return [
        w.strip(".,!?;:\"'()[]")
        for w in text.split()
        if w.strip()
    ]
