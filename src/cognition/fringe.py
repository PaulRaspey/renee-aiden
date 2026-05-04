"""
FringeState — slowly-updating low-dimensional state encoding conversational
direction. Updated between turns, before next input arrives. Anticipatory
bias for retrieval and response generation.

Jamesian fringe: the felt edge of attention around the focal content of
the moment. Tracks where the conversation is heading rather than what it
is presently about.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol
from uuid import uuid4

import numpy as np

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Turn — small joint object passed to scorers/trackers each update.
# ----------------------------------------------------------------------
@dataclass
class Turn:
    """One conversational exchange. Both sides included so scorers can
    weight them (assistant register choices are more deliberate than user
    transcription, so default weighting is 0.4 user / 0.6 assistant)."""
    user: str
    assistant: str
    mood: Optional[object] = None  # MoodState — kept loosely typed to avoid import cycle
    ts: datetime = field(default_factory=datetime.now)
    turn_id: str = field(default_factory=lambda: uuid4().hex)


# ----------------------------------------------------------------------
# OpenLoop — an unresolved thread the conversation has raised.
# ----------------------------------------------------------------------
@dataclass
class OpenLoop:
    loop_id: str
    salience: float
    last_touched_turn: int
    summary: str


# ----------------------------------------------------------------------
# Protocols — duck-typed dependencies of FringeState.update().
# ----------------------------------------------------------------------
class Embedder(Protocol):
    def embed(self, text: str) -> np.ndarray: ...


class AffectScorerProto(Protocol):
    def score(self, turn: Turn) -> np.ndarray: ...


class RegisterDetectorProto(Protocol):
    def detect(self, turn: Turn) -> np.ndarray: ...


class LoopTrackerProto(Protocol):
    def check(self, turn: Turn, turn_count: int) -> Optional[OpenLoop]: ...


class PressureComputerProto(Protocol):
    def compute(self, turn: Turn, topical_vector: np.ndarray, turn_count: int) -> float: ...


# ----------------------------------------------------------------------
# FringeState
# ----------------------------------------------------------------------
@dataclass
class FringeState:
    """
    Slowly-updating low-dimensional state encoding conversational direction.
    Updated between turns, before next user input arrives.
    """

    embedding_dim: int = 384  # MiniLM-L6-v2 default; matches MemoryStore.

    topical_vector: Optional[np.ndarray] = None
    affective_tilt: Optional[np.ndarray] = None    # 6-dim
    register: Optional[np.ndarray] = None          # 3-simplex
    open_loops: list[OpenLoop] = field(default_factory=list)
    temporal_pressure: float = 0.0

    last_updated: datetime = field(default_factory=datetime.now)
    turn_count: int = 0

    # Per-turn decay rates. Tunable; surfaced as class attributes so tests
    # can patch and so the values are visible when reading the class.
    TOPICAL_DECAY: float = 0.85
    AFFECTIVE_DECAY: float = 0.75
    LOOP_DECAY: float = 0.90
    REGISTER_INERTIA: float = 0.90  # higher = slower register changes

    def __post_init__(self):
        if self.topical_vector is None:
            self.topical_vector = np.zeros(self.embedding_dim, dtype=np.float32)
        if self.affective_tilt is None:
            self.affective_tilt = np.zeros(6, dtype=np.float32)
        if self.register is None:
            self.register = np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def update(
        self,
        turn: Turn,
        embedder: Embedder,
        affect_scorer: AffectScorerProto,
        register_detector: RegisterDetectorProto,
        loop_tracker: LoopTrackerProto,
        pressure_computer: PressureComputerProto,
    ) -> None:
        """Update fringe from a completed turn. Call after the turn settles,
        before the next user input. Failures are logged, never raised — a
        broken fringe must not break the turn."""
        self.turn_count += 1
        try:
            # Topical drift via EMA. Embed the joint user+assistant content.
            joint_text = f"{turn.user}\n{turn.assistant}".strip() or " "
            new_embedding = np.asarray(embedder.embed(joint_text), dtype=np.float32)
            if new_embedding.shape != self.topical_vector.shape:
                # Should not happen if embedding_dim was set correctly; coerce
                # rather than raise so a misconfig doesn't kill the turn.
                logger.warning(
                    "fringe topical embedding dim mismatch (%s vs %s); resizing",
                    new_embedding.shape, self.topical_vector.shape,
                )
                new_embedding = np.resize(new_embedding, self.topical_vector.shape)
            self.topical_vector = (
                self.TOPICAL_DECAY * self.topical_vector
                + (1 - self.TOPICAL_DECAY) * new_embedding
            ).astype(np.float32)

            # Affective tilt
            new_affect = np.asarray(affect_scorer.score(turn), dtype=np.float32)
            self.affective_tilt = (
                self.AFFECTIVE_DECAY * self.affective_tilt
                + (1 - self.AFFECTIVE_DECAY) * new_affect
            ).astype(np.float32)

            # Register (3-simplex; renormalize to stay on the simplex)
            detected_register = np.asarray(register_detector.detect(turn), dtype=np.float32)
            mixed = (
                self.REGISTER_INERTIA * self.register
                + (1 - self.REGISTER_INERTIA) * detected_register
            )
            total = float(mixed.sum())
            if total <= 0:
                self.register = np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32)
            else:
                self.register = (mixed / total).astype(np.float32)

            # Open loops: decay first, then maybe add a new one.
            self._decay_loops()
            new_loop = loop_tracker.check(turn, self.turn_count)
            if new_loop is not None:
                self.open_loops.append(new_loop)

            # Temporal pressure
            self.temporal_pressure = float(
                pressure_computer.compute(turn, self.topical_vector, self.turn_count)
            )
            self.temporal_pressure = max(-1.0, min(1.0, self.temporal_pressure))

            self.last_updated = datetime.now()

            logger.debug(
                "fringe updated: turn=%d register=%s pressure=%.2f loops=%d",
                self.turn_count, self._dominant_register(),
                self.temporal_pressure, len(self.open_loops),
            )
        except Exception as e:
            logger.error("fringe update failed: %s", e, exc_info=True)
            # swallow — fringe failure must not break the turn

    # ------------------------------------------------------------------
    # Decay-on-load (for cross-session continuity)
    # ------------------------------------------------------------------
    def decay_to_now(self) -> None:
        """Apply time-elapsed decay since last_updated. Call on load so a
        fringe that was last written hours ago isn't treated as fresh.
        Half-life ~13.5 hours via 0.95**hours_elapsed. Register stays — it's
        an identity-adjacent signal that shouldn't fade with the clock."""
        elapsed = (datetime.now() - self.last_updated).total_seconds() / 3600.0
        if elapsed < 0.1:  # under ~6 minutes, no-op
            return
        decay_factor = float(0.95 ** elapsed)
        self.topical_vector = (self.topical_vector * decay_factor).astype(np.float32)
        self.affective_tilt = (self.affective_tilt * decay_factor).astype(np.float32)
        self.temporal_pressure *= decay_factor
        for loop in self.open_loops:
            loop.salience *= decay_factor
        self.open_loops = [l for l in self.open_loops if l.salience > 0.1]
        # leave register alone; identity-slow signal
        self.last_updated = datetime.now()

    # ------------------------------------------------------------------
    # Outputs consumed by retrieval and prompt construction
    # ------------------------------------------------------------------
    def to_retrieval_bias(self) -> np.ndarray:
        """Vector for biasing memory retrieval. Caller decides the blend."""
        return self.topical_vector.copy()

    def to_prompt_prefix(self) -> str:
        """Natural-language fringe summary for prompt injection."""
        register_label = self._dominant_register()
        affect_summary = self._render_affect()
        loops_summary = self._render_open_loops()
        pressure_label = self._render_pressure()

        parts = [
            f"register tilting {register_label}",
            f"affect {affect_summary}",
            f"pace {pressure_label}",
        ]
        if loops_summary:
            parts.append(f"open threads: {loops_summary}")

        return f"[Conversational fringe: {'; '.join(parts)}.]"

    def to_attention_bias(self) -> dict:
        """Structured dict for callers that want raw signals."""
        return {
            "topical": self.topical_vector,
            "affective": self.affective_tilt,
            "register": self.register,
            "open_loops": [
                {"id": l.loop_id, "salience": l.salience, "summary": l.summary}
                for l in self.open_loops
            ],
            "pressure": self.temporal_pressure,
            "turn_count": self.turn_count,
        }

    def reset(self) -> None:
        """Reset to initial state. Used for new conversation sessions."""
        self.topical_vector = np.zeros(self.embedding_dim, dtype=np.float32)
        self.affective_tilt = np.zeros(6, dtype=np.float32)
        self.register = np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32)
        self.open_loops = []
        self.temporal_pressure = 0.0
        self.turn_count = 0
        self.last_updated = datetime.now()

    # ------------------------------------------------------------------
    # JSON serialization (numpy arrays as lists)
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "embedding_dim": int(self.embedding_dim),
            "topical_vector": self.topical_vector.tolist(),
            "affective_tilt": self.affective_tilt.tolist(),
            "register": self.register.tolist(),
            "open_loops": [
                {
                    "loop_id": l.loop_id,
                    "salience": float(l.salience),
                    "last_touched_turn": int(l.last_touched_turn),
                    "summary": l.summary,
                }
                for l in self.open_loops
            ],
            "temporal_pressure": float(self.temporal_pressure),
            "last_updated": self.last_updated.isoformat(),
            "turn_count": int(self.turn_count),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FringeState":
        dim = int(data.get("embedding_dim", 384))
        state = cls(embedding_dim=dim)
        tv = data.get("topical_vector")
        if tv is not None:
            state.topical_vector = np.asarray(tv, dtype=np.float32)
            if state.topical_vector.shape[0] != dim:
                state.topical_vector = np.resize(state.topical_vector, (dim,)).astype(np.float32)
        at = data.get("affective_tilt")
        if at is not None:
            state.affective_tilt = np.asarray(at, dtype=np.float32)
        reg = data.get("register")
        if reg is not None:
            state.register = np.asarray(reg, dtype=np.float32)
        state.open_loops = [
            OpenLoop(
                loop_id=l["loop_id"],
                salience=float(l["salience"]),
                last_touched_turn=int(l["last_touched_turn"]),
                summary=l["summary"],
            )
            for l in data.get("open_loops", [])
        ]
        state.temporal_pressure = float(data.get("temporal_pressure", 0.0))
        last = data.get("last_updated")
        if last:
            try:
                state.last_updated = datetime.fromisoformat(last)
            except Exception:
                state.last_updated = datetime.now()
        state.turn_count = int(data.get("turn_count", 0))
        return state

    # ------------------------------------------------------------------
    # private renderers
    # ------------------------------------------------------------------
    def _decay_loops(self) -> None:
        for loop in self.open_loops:
            loop.salience *= self.LOOP_DECAY
        self.open_loops = [l for l in self.open_loops if l.salience > 0.1]

    def _dominant_register(self) -> str:
        labels = ["technical", "intimate", "playful"]
        return labels[int(np.argmax(self.register))]

    def _render_affect(self) -> str:
        labels = ["sharpening", "softening", "opening", "closing", "warming", "cooling"]
        active = [
            labels[i] for i in range(6)
            if float(self.affective_tilt[i]) > 0.3
        ]
        return ", ".join(active) if active else "neutral"

    def _render_open_loops(self) -> str:
        top = sorted(self.open_loops, key=lambda l: l.salience, reverse=True)[:3]
        return ", ".join(l.summary for l in top)

    def _render_pressure(self) -> str:
        if self.temporal_pressure > 0.3:
            return "building"
        if self.temporal_pressure < -0.3:
            return "wandering"
        return "steady"
