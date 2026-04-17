"""
Predictive endpointer (M8).

Heuristic turn-end predictor. Runs every ~100ms during user speech and
produces a probability that the user is about to finish their current turn,
plus a discrete action (idle / prewarm / speculative / commit) that the
orchestrator can switch on.

Replaces the 100M-param neural endpointer sketched in
architecture/05_turn_taking.md. We can swap this for a model later without
changing the consumer API.

Inputs per tick:
  transcript: latest partial or final ASR output.
  silence_ms: duration of trailing silence in milliseconds.
  energy_falling: optional prosodic marker (pitch/energy trending down).

Output: float probability in [0, 1] or a full `EndpointDecision` that
includes the discrete action.

Thresholds match the architecture doc:
  p > 0.5         -> prewarm persona core
  p > 0.7         -> start speculative generation
  p > 0.9 for 150ms -> commit to response
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EndpointAction(str, Enum):
    IDLE = "idle"
    PREWARM = "prewarm"
    SPECULATIVE = "speculative"
    COMMIT = "commit"


@dataclass
class EndpointDecision:
    p_end: float
    action: EndpointAction
    reason: str
    sustain_ms: int = 0


TERMINAL_PUNCTS: tuple[str, ...] = (".", "!", "?")

# Words that, at the tail of a partial transcript, strongly imply more is coming.
INCOMPLETE_CONTINUATIONS: frozenset[str] = frozenset({
    "and", "but", "so", "or", "because", "if", "when", "while",
    "since", "although", "though", "until", "unless", "that",
    "which", "who", "what", "where", "why", "how",
    "like",  # "...and then like..."
})

# Speech disfluencies that typically precede continuation.
FILLERS_END: frozenset[str] = frozenset({"uh", "um", "er", "hmm", "mm", "ah", "eh"})


class Endpointer:
    """
    One instance per conversation. Call `decide(...)` every audio tick.

    The sustain timer for the commit action is internal; caller doesn't
    need to track it. `reset()` on turn boundary to clear sustain.
    """

    def __init__(
        self,
        *,
        silence_prewarm_ms: int = 300,
        silence_speculative_ms: int = 500,
        silence_commit_ms: int = 800,
        commit_min_sustain_ms: int = 150,
    ):
        self.silence_prewarm_ms = silence_prewarm_ms
        self.silence_speculative_ms = silence_speculative_ms
        self.silence_commit_ms = silence_commit_ms
        self.commit_min_sustain_ms = commit_min_sustain_ms
        self._sustain_timer_ms: int = 0

    # ------------------------------------------------------------------
    # scoring
    # ------------------------------------------------------------------

    def predict(
        self,
        transcript: str,
        silence_ms: int,
        *,
        energy_falling: bool = False,
    ) -> float:
        silence_ms = max(0, int(silence_ms))

        # Silence contribution is a piecewise ramp.
        if silence_ms < 150:
            p = silence_ms / 1500.0
        elif silence_ms < self.silence_prewarm_ms:
            span = max(1, self.silence_prewarm_ms - 150)
            p = 0.10 + (silence_ms - 150) / span * 0.25
        elif silence_ms < self.silence_speculative_ms:
            span = max(1, self.silence_speculative_ms - self.silence_prewarm_ms)
            p = 0.35 + (silence_ms - self.silence_prewarm_ms) / span * 0.30
        elif silence_ms < self.silence_commit_ms:
            span = max(1, self.silence_commit_ms - self.silence_speculative_ms)
            p = 0.65 + (silence_ms - self.silence_speculative_ms) / span * 0.25
        else:
            p = 0.90 + min(0.09, (silence_ms - self.silence_commit_ms) / 3000.0)

        # Transcript completeness.
        text = transcript.strip()
        if text:
            tail_word = _last_word(text)
            if text.endswith(TERMINAL_PUNCTS):
                p += 0.15
            elif text.endswith(","):
                p -= 0.25
            if tail_word in INCOMPLETE_CONTINUATIONS:
                p -= 0.30
            if tail_word in FILLERS_END:
                p -= 0.20
            # A barely-started sentence with short silence is probably not the
            # end — unless it already has terminal punctuation ("Sure."),
            # which signals a complete one-word turn.
            word_count = len(text.split())
            if (
                word_count < 3
                and silence_ms < 400
                and not text.endswith(TERMINAL_PUNCTS)
            ):
                p -= 0.10
        else:
            # No transcript observed at all; treat as pure silence.
            p -= 0.15

        if energy_falling:
            p += 0.08

        return max(0.0, min(1.0, p))

    # ------------------------------------------------------------------
    # action selection
    # ------------------------------------------------------------------

    def decide(
        self,
        transcript: str,
        silence_ms: int,
        *,
        energy_falling: bool = False,
        tick_elapsed_ms: int = 100,
    ) -> EndpointDecision:
        p = self.predict(transcript, silence_ms, energy_falling=energy_falling)
        if p >= 0.9:
            self._sustain_timer_ms += max(0, int(tick_elapsed_ms))
        else:
            self._sustain_timer_ms = 0

        if p >= 0.9 and self._sustain_timer_ms >= self.commit_min_sustain_ms:
            return EndpointDecision(
                p_end=p, action=EndpointAction.COMMIT,
                reason="p>=0.9 sustained", sustain_ms=self._sustain_timer_ms,
            )
        if p >= 0.7:
            return EndpointDecision(
                p_end=p, action=EndpointAction.SPECULATIVE,
                reason="p>=0.7 speculative", sustain_ms=self._sustain_timer_ms,
            )
        if p >= 0.5:
            return EndpointDecision(
                p_end=p, action=EndpointAction.PREWARM,
                reason="p>=0.5 prewarm", sustain_ms=self._sustain_timer_ms,
            )
        return EndpointDecision(
            p_end=p, action=EndpointAction.IDLE,
            reason="below prewarm threshold", sustain_ms=self._sustain_timer_ms,
        )

    def reset(self) -> None:
        self._sustain_timer_ms = 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[A-Za-z']+")


def _last_word(text: str) -> str:
    words = _WORD_RE.findall(text.lower())
    return words[-1] if words else ""
