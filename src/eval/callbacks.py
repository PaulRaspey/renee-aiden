"""
Callback accuracy tracker (M11).

Each turn where retrieved_memories is non-empty is a potential callback
opportunity. We log the opportunity and whether the response actually
surfaced content from the retrieved memory. Rolling accuracy feeds the
dashboard.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .scorers import score_callback_hit


@dataclass
class CallbackEvent:
    turn_id: str
    ts: float
    retrieved_count: int
    hit: bool
    matches_json: str


class CallbackTracker:
    def __init__(self, db_path: Path | str = "state/callbacks.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS callback_events (
                    turn_id TEXT PRIMARY KEY,
                    ts REAL,
                    retrieved_count INTEGER,
                    hit INTEGER,
                    matches_json TEXT
                )
                """
            )

    def log_turn(
        self,
        turn_id: str,
        response_text: str,
        retrieved_memories: Optional[Iterable[dict]],
    ) -> Optional[CallbackEvent]:
        mems = list(retrieved_memories or [])
        if not mems:
            return None
        result = score_callback_hit(response_text, mems)
        ev = CallbackEvent(
            turn_id=turn_id,
            ts=time.time(),
            retrieved_count=len(mems),
            hit=bool(result.passed),
            matches_json=json.dumps(result.details.get("matches", [])),
        )
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO callback_events VALUES (?, ?, ?, ?, ?)
                """,
                (ev.turn_id, ev.ts, ev.retrieved_count, int(ev.hit), ev.matches_json),
            )
        return ev

    def accuracy(self, *, since_ts: float = 0.0) -> dict:
        with sqlite3.connect(self.db_path) as con:
            rows = list(con.execute(
                "SELECT hit FROM callback_events WHERE ts >= ?",
                (since_ts,),
            ))
        if not rows:
            return {"opportunities": 0, "hits": 0, "accuracy": 0.0}
        hits = sum(1 for (h,) in rows if int(h) == 1)
        total = len(rows)
        return {
            "opportunities": total,
            "hits": hits,
            "accuracy": round(hits / total, 3),
        }
