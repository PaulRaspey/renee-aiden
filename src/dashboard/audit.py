"""Dashboard action audit log.

Every write performed through the dashboard is persisted here so PJ has
a record of what he changed, when, and to what value. This is his audit
trail on himself.

Schema (SQLite at state/dashboard_actions.db):
  actions(id, ts, field, old_value, new_value, confirmed, actor, receipt_id)

The `receipt_id` column is the UAHP completion receipt the dashboard
agent signed for the action. The dashboard process is a distinct UAHP
agent (see src/dashboard/agent.py) so config edits carry a provenance
chain independent of the persona core.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class ActionRecord:
    ts: float
    field: str
    old_value: str
    new_value: str
    confirmed: bool
    actor: str
    receipt_id: str = ""

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "confirmed": self.confirmed,
            "actor": self.actor,
            "receipt_id": self.receipt_id,
        }


class DashboardAuditLog:
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
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    field TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    confirmed INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    receipt_id TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def record(
        self,
        *,
        field: str,
        old_value: Any,
        new_value: Any,
        confirmed: bool,
        actor: str,
        receipt_id: str = "",
    ) -> ActionRecord:
        rec = ActionRecord(
            ts=time.time(),
            field=field,
            old_value=_stringify(old_value),
            new_value=_stringify(new_value),
            confirmed=bool(confirmed),
            actor=actor,
            receipt_id=receipt_id,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO actions (ts, field, old_value, new_value, "
                "confirmed, actor, receipt_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.ts,
                    rec.field,
                    rec.old_value,
                    rec.new_value,
                    1 if rec.confirmed else 0,
                    rec.actor,
                    rec.receipt_id,
                ),
            )
        return rec

    def recent(self, limit: int = 50) -> list[ActionRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, field, old_value, new_value, confirmed, actor, "
                "receipt_id FROM actions ORDER BY ts DESC LIMIT ?",
                (int(max(1, limit)),),
            ).fetchall()
        return [
            ActionRecord(
                ts=float(r[0]),
                field=str(r[1]),
                old_value=str(r[2]),
                new_value=str(r[3]),
                confirmed=bool(r[4]),
                actor=str(r[5]),
                receipt_id=str(r[6]),
            )
            for r in rows
        ]

    def count(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM actions").fetchone()
        return int(row[0] if row else 0)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)
