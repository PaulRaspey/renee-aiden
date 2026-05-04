"""
FringeStore — JSON persistence for FringeState.

Per-persona file at <state_dir>/<persona>_fringe.json. State is read at
PersonaCore construction (with decay_to_now applied), written after each
fringe update so the next session resumes from a soft-attenuated version
of the last conversation rather than a cold start.

Match for MoodStore's structure: tied to a persona_name, anchored on a
state directory passed in by the caller.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .fringe import FringeState

logger = logging.getLogger(__name__)


class FringeStore:
    """JSON-backed fringe persistence for one persona."""

    def __init__(self, persona_name: str, state_dir: Path):
        self.persona_name = persona_name.lower()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / f"{self.persona_name}_fringe.json"

    def load(self, embedding_dim: int = 384) -> FringeState:
        """Load fringe from disk, applying decay_to_now. Returns a fresh
        FringeState if no file exists or load fails."""
        if not self.path.exists():
            return FringeState(embedding_dim=embedding_dim)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            state = FringeState.from_dict(data)
            if state.embedding_dim != embedding_dim:
                # Embedder dim changed under us — keep the persisted state
                # so we don't drop everything, but log the mismatch. The
                # update() path safely resizes embeddings if they don't
                # match the stored topical_vector.
                logger.warning(
                    "fringe %s loaded with dim=%d but caller expects dim=%d; keeping persisted",
                    self.persona_name, state.embedding_dim, embedding_dim,
                )
            state.decay_to_now()
            return state
        except Exception as e:
            logger.error("fringe load failed for %s: %s; returning fresh state", self.persona_name, e)
            return FringeState(embedding_dim=embedding_dim)

    def save(self, state: FringeState) -> None:
        """Persist fringe to disk. Write-then-rename for atomicity so a
        crash mid-write can't leave a half-written file."""
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(state.to_dict()), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            logger.error("fringe save failed for %s: %s", self.persona_name, e)
