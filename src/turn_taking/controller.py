"""
Turn controller: state machine that wires endpointer + latency + interruption
(M8). Sits between the audio I/O layer and the persona core; M10's
orchestrator consumes this as the turn-timing layer.

States follow architecture/05_turn_taking.md:
  IDLE            -- no one is speaking
  USER_SPEAKING   -- user audio flowing; endpointer ticks
  RENEE_PREPARING -- post-endpoint, waiting out the target latency
  RENEE_SPEAKING  -- TTS active; user-interrupt detector runs
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .endpointer import Endpointer, EndpointAction, EndpointDecision
from .interruption import InterruptionEvent, InterruptionHandler
from .latency import LatencyPlan, plan_latency


class TurnState(str, Enum):
    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    RENEE_PREPARING = "renee_preparing"
    RENEE_SPEAKING = "renee_speaking"


@dataclass
class TickResult:
    state: TurnState
    endpoint: Optional[EndpointDecision] = None
    interruption: Optional[InterruptionEvent] = None


class TurnController:
    def __init__(
        self,
        *,
        endpointer: Optional[Endpointer] = None,
        interruption: Optional[InterruptionHandler] = None,
    ):
        self.state: TurnState = TurnState.IDLE
        self.endpointer = endpointer or Endpointer()
        self.interruption = interruption or InterruptionHandler()

    # ------------------------------------------------------------------
    # user side
    # ------------------------------------------------------------------

    def on_user_tick(
        self,
        transcript: str,
        silence_ms: int,
        *,
        energy: float = 0.0,
        energy_falling: bool = False,
        tick_ms: int = 100,
    ) -> TickResult:
        # If renee is speaking and user audio energy crosses threshold -> interruption.
        if self.state == TurnState.RENEE_SPEAKING:
            event = self.interruption.observe_user_energy(energy)
            if event is not None:
                self.state = TurnState.USER_SPEAKING
                self.endpointer.reset()
                return TickResult(state=self.state, interruption=event)
            # still renee speaking, user not yet interrupting
            return TickResult(state=self.state)

        self.state = TurnState.USER_SPEAKING
        decision = self.endpointer.decide(
            transcript, silence_ms,
            energy_falling=energy_falling, tick_elapsed_ms=tick_ms,
        )
        return TickResult(state=self.state, endpoint=decision)

    # ------------------------------------------------------------------
    # response planning
    # ------------------------------------------------------------------

    def plan_response_latency(
        self,
        user_text: str,
        mood: Any = None,
        *,
        context_flags: Optional[dict] = None,
    ) -> LatencyPlan:
        return plan_latency(user_text, mood, context_flags=context_flags)

    def begin_renee_preparing(self) -> None:
        self.state = TurnState.RENEE_PREPARING

    def begin_renee_speaking(self) -> None:
        self.state = TurnState.RENEE_SPEAKING

    def end_renee_turn(self) -> None:
        self.state = TurnState.IDLE
        self.endpointer.reset()
        self.interruption.on_turn_boundary()
