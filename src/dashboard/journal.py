"""M15 journal: immersion-break and 'too real' tag storage.

Kept separate from the health DB so the tags are easy to export and
replay alongside the conversation log. Tags land in
state/m15_journal.db.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


TAG_IMMERSION_BREAK = "immersion_break"
TAG_HIT = "hit"
TAG_PAUSE = "pause_24h"


@dataclass
class JournalEntry:
    id: int
    ts: float
    tag: str
    day_key: str
    turn_ts: Optional[float]
    note: str


class M15Journal:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    tag TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    turn_ts REAL,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def tag(
        self,
        *,
        tag: str,
        day_key: str,
        turn_ts: Optional[float] = None,
        note: str = "",
    ) -> JournalEntry:
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO entries (ts, tag, day_key, turn_ts, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, tag, day_key, turn_ts, note),
            )
            row_id = int(cur.lastrowid or 0)
        return JournalEntry(
            id=row_id, ts=now, tag=tag, day_key=day_key, turn_ts=turn_ts, note=note,
        )

    def entries_for_day(self, day_key: str) -> list[JournalEntry]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, tag, day_key, turn_ts, note FROM entries "
                "WHERE day_key=? ORDER BY ts",
                (day_key,),
            ).fetchall()
        return [
            JournalEntry(
                id=int(r[0]),
                ts=float(r[1]),
                tag=str(r[2]),
                day_key=str(r[3]),
                turn_ts=float(r[4]) if r[4] is not None else None,
                note=str(r[5] or ""),
            )
            for r in rows
        ]

    def counts_by_tag(self, *, days: int = 30) -> dict[str, int]:
        cutoff = time.time() - days * 86400.0
        with self._conn() as c:
            rows = c.execute(
                "SELECT tag, COUNT(*) FROM entries WHERE ts >= ? GROUP BY tag",
                (cutoff,),
            ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}
