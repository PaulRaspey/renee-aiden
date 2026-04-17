"""
Two-way interruption handler (M8).

User interrupts Renée:
  * Detect sustained user voice energy crossing a threshold while Renée is
    speaking. Emit an event that cancels Renée's remaining TTS and marks a
    graceful yield ("yeah?" / "sorry, go on").
  * The architecture target is to cancel within 100ms. We require a short
    sustain window (default 100ms) so a stray cough doesn't kill a turn.

Renée interrupts user:
  * Triggers: strong disagreement, urgent correction, high excitement, or
    callback urgency (a pattern she wants to close the loop on).
  * Capped at 1 interruption per rolling window of N turns (default 10).
    Otherwise she reads as twitchy.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class InterruptionReason(str, Enum):
    USER_VOICE_ENERGY = "user_voice_energy"
    RENEE_DISAGREEMENT = "renee_disagreement"
    RENEE_CORRECTION = "renee_correction"
    RENEE_EXCITEMENT = "renee_excitement"
    RENEE_CALLBACK_URGENCY = "renee_callback_urgency"


@dataclass
class InterruptionEvent:
    who: str                 # 'user' | 'renee'
    reason: str
    at_ms: float
    cancel_tts: bool = False     # set when user interrupts renee
    yield_gracefully: bool = False  # renee should yield with a soft token


class InterruptionHandler:
    """
    Stateful. Created per conversation. `on_turn_boundary()` after every
    completed turn to advance the rolling window used for Renée's cap.
    """

    def __init__(
        self,
        *,
        cap_per_n_turns: int = 10,
        user_energy_threshold: float = 0.45,
        sustain_required_ms: int = 100,
    ):
        self.cap = cap_per_n_turns
        self.user_energy_threshold = user_energy_threshold
        self.sustain_required_ms = sustain_required_ms
        self._renee_interrupt_turns: deque[int] = deque()
        self._turn_counter: int = 0
        self._user_above_since_ms: Optional[float] = None

    # ------------------------------------------------------------------
    # turn bookkeeping
    # ------------------------------------------------------------------

    def on_turn_boundary(self) -> None:
        self._turn_counter += 1
        cutoff = self._turn_counter - self.cap
        while self._renee_interrupt_turns and self._renee_interrupt_turns[0] <= cutoff:
            self._renee_interrupt_turns.popleft()

    def renee_interruption_count_in_window(self) -> int:
        return len(self._renee_interrupt_turns)

    def reset(self) -> None:
        self._renee_interrupt_turns.clear()
        self._turn_counter = 0
        self._user_above_since_ms = None

    # ------------------------------------------------------------------
    # user interrupts renee
    # ------------------------------------------------------------------

    def observe_user_energy(
        self,
        energy: float,
        *,
        now_ms: Optional[float] = None,
    ) -> Optional[InterruptionEvent]:
        """
        Call per audio frame while Renée is speaking. Returns an event once
        user energy has been above threshold for `sustain_required_ms`.
        """
        now = now_ms if now_ms is not None else time.time() * 1000.0
        if energy >= self.user_energy_threshold:
            if self._user_above_since_ms is None:
                self._user_above_since_ms = now
                return None
            sustained = now - self._user_above_since_ms
            if sustained >= self.sustain_required_ms:
                self._user_above_since_ms = None
                return InterruptionEvent(
                    who="user",
                    reason=InterruptionReason.USER_VOICE_ENERGY.value,
                    at_ms=now,
                    cancel_tts=True,
                    yield_gracefully=True,
                )
            return None
        # energy dropped below threshold; cancel any sustain timer
        self._user_above_since_ms = None
        return None

    # ------------------------------------------------------------------
    # renee interrupts user
    # ------------------------------------------------------------------

    def should_renee_interrupt(
        self,
        *,
        user_speaking: bool,
        disagreement_score: float = 0.0,
        correction_urgency: float = 0.0,
        excitement_score: float = 0.0,
        callback_urgency: float = 0.0,
        now_ms: Optional[float] = None,
    ) -> Optional[InterruptionEvent]:
        if not user_speaking:
            return None
        if len(self._renee_interrupt_turns) >= self.cap:
            return None

        reason: Optional[str] = None
        if disagreement_score >= 0.8:
            reason = InterruptionReason.RENEE_DISAGREEMENT.value
        elif correction_urgency >= 0.8:
            reason = InterruptionReason.RENEE_CORRECTION.value
        elif excitement_score >= 0.85:
            reason = InterruptionReason.RENEE_EXCITEMENT.value
        elif callback_urgency >= 0.85:
            reason = InterruptionReason.RENEE_CALLBACK_URGENCY.value
        if reason is None:
            return None

        now = now_ms if now_ms is not None else time.time() * 1000.0
        self._renee_interrupt_turns.append(self._turn_counter)
        return InterruptionEvent(
            who="renee",
            reason=reason,
            at_ms=now,
            cancel_tts=False,
            yield_gracefully=False,
        )
