"""
Append-only JSONL trace logging for FringeState A/B evaluation.

Records one line per turn capturing the fringe state at the moment it
influenced the prompt and retrieval. Pure write-side instrumentation:
never reads, never parses, never analyzes.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


_AFFECT_LABELS = (
    "sharpening", "softening", "opening",
    "closing", "warming", "cooling",
)
_REGISTER_LABELS = ("technical", "intimate", "playful")
_AFFECT_THRESHOLD = 0.3


class FringeTracer:
    """Append-only JSONL writer for fringe traces.

    One file per persona per day at <base_path>/<persona>_<YYYY-MM-DD>.jsonl.
    Created lazily on first write. Failures are logged at WARNING but
    never raised: tracing must not break turns.
    """

    def __init__(self, base_path: Optional[str] = None):
        self.base_path = Path(
            base_path or os.getenv("FRINGE_TRACE_PATH", "data/fringe_traces/")
        )

    def trace(
        self,
        persona_name: str,
        turn_id: str,
        fringe_state,
        prompt_prefix: str,
    ) -> None:
        """Append a single trace line for this turn.

        Silent on success. Logs at WARNING on failure, never raises.
        """
        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            filepath = self.base_path / f"{persona_name}_{today}.jsonl"

            record = {
                "turn_id": turn_id,
                "ts": datetime.now().isoformat(),
                "persona": persona_name,
                "prefix": prompt_prefix,
                "dominant_register": self._dominant_register(fringe_state),
                "register_distribution": _to_list(fringe_state.register),
                "pressure": float(fringe_state.temporal_pressure),
                "n_open_loops": len(fringe_state.open_loops),
                "open_loop_summaries": [l.summary for l in fringe_state.open_loops],
                "affect_dominant": self._dominant_affect(fringe_state),
                "turn_count": int(fringe_state.turn_count),
            }

            with filepath.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(
                "fringe trace write failed for %s/%s: %s",
                persona_name, turn_id, e,
            )
            # Never raise: trace logging must not break the turn.

    @staticmethod
    def _dominant_register(fringe_state) -> str:
        return _REGISTER_LABELS[int(np.argmax(fringe_state.register))]

    @staticmethod
    def _dominant_affect(fringe_state) -> list[str]:
        return [
            _AFFECT_LABELS[i]
            for i in range(len(_AFFECT_LABELS))
            if float(fringe_state.affective_tilt[i]) > _AFFECT_THRESHOLD
        ]


def _to_list(arr) -> list:
    """Convert a numpy array (or any sequence) to a plain Python list of
    floats so json.dumps doesn't trip on np.float32/np.float64 instances."""
    if hasattr(arr, "tolist"):
        return arr.tolist()
    return [float(x) for x in arr]
