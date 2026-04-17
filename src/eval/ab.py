"""
Blind A/B test queue (M11).

Queues two responses to the same prompt, random-labeled A/B so PJ can't
tell which is the candidate build vs the baseline. Ratings write to
SQLite at `state/ab.db`. `win_rate(label)` returns the fraction of
ratings where the labeled side was preferred.

This module has no UI; the CLI layer wraps `next_pair`, `record_rating`,
and `win_rate`.
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ABPair:
    pair_id: str
    prompt: str
    option_a: str
    option_b: str
    label_a: str             # e.g. "candidate"
    label_b: str             # e.g. "baseline"
    created_at: float = field(default_factory=lambda: time.time())
    meta: dict = field(default_factory=dict)


@dataclass
class ABRating:
    pair_id: str
    chosen: str              # 'a' | 'b'
    margin: int = 3          # 1..5 strength of preference
    notes: str = ""
    rated_at: float = field(default_factory=lambda: time.time())
    rater: str = "pj"


class ABQueue:
    def __init__(self, db_path: Path | str = "state/ab.db",
                 rng: Optional[random.Random] = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.rng = rng or random.Random()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS ab_pairs (
                    pair_id TEXT PRIMARY KEY,
                    prompt TEXT,
                    option_a TEXT,
                    option_b TEXT,
                    label_a TEXT,
                    label_b TEXT,
                    created_at REAL,
                    meta_json TEXT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS ab_ratings (
                    pair_id TEXT,
                    chosen TEXT,
                    margin INTEGER,
                    notes TEXT,
                    rated_at REAL,
                    rater TEXT,
                    PRIMARY KEY (pair_id, rater)
                )
                """
            )

    # ------------------------------------------------------------------
    # queue ops
    # ------------------------------------------------------------------

    def queue_pair(
        self,
        *,
        prompt: str,
        candidate: str,
        baseline: str,
        meta: Optional[dict] = None,
    ) -> ABPair:
        """Coin-flip which side gets 'a' vs 'b' so raters can't inside-track."""
        pair_id = str(uuid.uuid4())
        if self.rng.random() < 0.5:
            option_a, option_b = candidate, baseline
            label_a, label_b = "candidate", "baseline"
        else:
            option_a, option_b = baseline, candidate
            label_a, label_b = "baseline", "candidate"
        pair = ABPair(
            pair_id=pair_id,
            prompt=prompt,
            option_a=option_a,
            option_b=option_b,
            label_a=label_a,
            label_b=label_b,
            meta=meta or {},
        )
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO ab_pairs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pair.pair_id, pair.prompt, pair.option_a, pair.option_b,
                    pair.label_a, pair.label_b, pair.created_at,
                    json.dumps(pair.meta),
                ),
            )
        return pair

    def next_pair(self, rater: str = "pj") -> Optional[ABPair]:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                """
                SELECT pair_id, prompt, option_a, option_b, label_a, label_b,
                       created_at, meta_json
                FROM ab_pairs
                WHERE pair_id NOT IN (SELECT pair_id FROM ab_ratings WHERE rater = ?)
                ORDER BY created_at ASC LIMIT 1
                """,
                (rater,),
            ).fetchone()
        if row is None:
            return None
        return ABPair(
            pair_id=row[0], prompt=row[1], option_a=row[2], option_b=row[3],
            label_a=row[4], label_b=row[5], created_at=row[6],
            meta=json.loads(row[7] or "{}"),
        )

    def record_rating(
        self,
        pair_id: str,
        chosen: str,
        *,
        margin: int = 3,
        notes: str = "",
        rater: str = "pj",
    ) -> ABRating:
        chosen = chosen.lower()
        if chosen not in ("a", "b"):
            raise ValueError(f"chosen must be 'a' or 'b', got {chosen!r}")
        margin = max(1, min(5, int(margin)))
        rating = ABRating(
            pair_id=pair_id, chosen=chosen, margin=margin, notes=notes, rater=rater,
        )
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO ab_ratings
                (pair_id, chosen, margin, notes, rated_at, rater)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (rating.pair_id, rating.chosen, rating.margin,
                 rating.notes, rating.rated_at, rating.rater),
            )
        return rating

    # ------------------------------------------------------------------
    # aggregate
    # ------------------------------------------------------------------

    def win_rate(self, label: str = "candidate") -> dict:
        with sqlite3.connect(self.db_path) as con:
            rows = list(con.execute(
                """
                SELECT p.label_a, p.label_b, r.chosen, r.margin
                FROM ab_pairs p JOIN ab_ratings r ON r.pair_id = p.pair_id
                """
            ))
        if not rows:
            return {"ratings": 0, "wins": 0, "win_rate": 0.0, "margin_sum": 0}
        wins = 0
        margin_sum = 0
        for la, lb, chosen, margin in rows:
            picked = la if chosen == "a" else lb
            if picked == label:
                wins += 1
                margin_sum += margin
        total = len(rows)
        return {
            "ratings": total,
            "wins": wins,
            "win_rate": round(wins / total, 3),
            "margin_sum": margin_sum,
            "label": label,
        }

    def pending_count(self, rater: str = "pj") -> int:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                """
                SELECT COUNT(*) FROM ab_pairs
                WHERE pair_id NOT IN (SELECT pair_id FROM ab_ratings WHERE rater = ?)
                """,
                (rater,),
            ).fetchone()
        return int(row[0]) if row else 0
