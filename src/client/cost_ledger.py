"""Pod-up cost ledger.

The ``/api/cost`` endpoint shows the *current* pod-up cost. Without
historical context that's only useful while a session is live. This
module persists pod up/down events to a small SQLite file under
``state/`` and aggregates them into per-day / per-month totals so the
dashboard can answer "how much have we spent this month vs the
``cloud.monthly_budget_usd: 500`` cap?".

Schema is intentionally tiny — one table — because billing data is
write-once-then-read and we don't need joins.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_LEDGER_PATH = Path("state") / "cost_ledger.db"


@dataclass
class PodEvent:
    """One pod up- or down-event. ``minutes`` is the elapsed time since the
    matching up-event for down rows; for up events it's 0."""
    pod_id: str
    event: str            # 'up' | 'down'
    timestamp_iso: str    # UTC ISO-8601
    gpu_type: str
    minutes: float        # elapsed since up (0 for up rows)
    hourly_usd: float
    cost_usd: float       # 0 for up rows
    note: str = ""


@dataclass
class CostBuckets:
    today_usd: float
    this_month_usd: float
    this_month_minutes: float
    monthly_budget_usd: Optional[float]
    over_budget: bool
    samples: int


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pod_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pod_id TEXT NOT NULL,
            event TEXT NOT NULL,
            timestamp_iso TEXT NOT NULL,
            gpu_type TEXT,
            minutes REAL NOT NULL DEFAULT 0,
            hourly_usd REAL NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            note TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pod_events_ts ON pod_events(timestamp_iso)",
    )
    conn.commit()
    return conn


@contextmanager
def _open(db_path: Optional[Path] = None):
    path = db_path or DEFAULT_LEDGER_PATH
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def record_up(
    *,
    pod_id: str,
    gpu_type: str = "",
    hourly_usd: float = 0.0,
    note: str = "",
    db_path: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
) -> int:
    """Append an up-event. Returns the row id."""
    when = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat()
    with _open(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO pod_events (pod_id, event, timestamp_iso, gpu_type, "
            "minutes, hourly_usd, cost_usd, note) VALUES (?, 'up', ?, ?, 0, ?, 0, ?)",
            (pod_id, when, gpu_type, hourly_usd, note),
        )
        conn.commit()
        return cur.lastrowid


def record_down(
    *,
    pod_id: str,
    minutes: float,
    hourly_usd: float,
    gpu_type: str = "",
    note: str = "",
    db_path: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
) -> int:
    """Append a down-event with the elapsed minutes since the matching up
    event. Caller computes ``minutes`` so we don't depend on the live pod
    being reachable at down time."""
    when = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat()
    cost = (minutes / 60.0) * hourly_usd
    with _open(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO pod_events (pod_id, event, timestamp_iso, gpu_type, "
            "minutes, hourly_usd, cost_usd, note) VALUES (?, 'down', ?, ?, ?, ?, ?, ?)",
            (pod_id, when, gpu_type, minutes, hourly_usd, cost, note),
        )
        conn.commit()
        return cur.lastrowid


def list_events(
    *,
    db_path: Optional[Path] = None,
    limit: int = 200,
) -> list[PodEvent]:
    with _open(db_path) as conn:
        rows = conn.execute(
            "SELECT pod_id, event, timestamp_iso, gpu_type, minutes, "
            "hourly_usd, cost_usd, COALESCE(note, '') FROM pod_events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [PodEvent(*r) for r in rows]


def buckets(
    *,
    monthly_budget_usd: Optional[float] = None,
    db_path: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
) -> CostBuckets:
    """Sum cost_usd grouped today / this month, in UTC. Use the operator's
    locale offsets at the dashboard level — this stays UTC for stability."""
    moment = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(_dt.timezone.utc)
    today_prefix = moment.strftime("%Y-%m-%d")
    month_prefix = moment.strftime("%Y-%m")
    with _open(db_path) as conn:
        today = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM pod_events "
            "WHERE event = 'down' AND substr(timestamp_iso, 1, 10) = ?",
            (today_prefix,),
        ).fetchone()[0] or 0.0
        month_cost, month_min, samples = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(minutes), 0), COUNT(*) "
            "FROM pod_events WHERE event = 'down' AND substr(timestamp_iso, 1, 7) = ?",
            (month_prefix,),
        ).fetchone()
    over = bool(
        monthly_budget_usd is not None
        and (month_cost or 0.0) > float(monthly_budget_usd)
    )
    return CostBuckets(
        today_usd=round(float(today), 2),
        this_month_usd=round(float(month_cost or 0.0), 2),
        this_month_minutes=round(float(month_min or 0.0), 1),
        monthly_budget_usd=monthly_budget_usd,
        over_budget=over,
        samples=int(samples or 0),
    )


def reset_for_test(db_path: Path) -> None:
    """Drop the table so each test starts clean. Tests only — never call
    in production."""
    with _open(db_path) as conn:
        conn.execute("DELETE FROM pod_events")
        conn.commit()


__all__ = [
    "PodEvent", "CostBuckets",
    "record_up", "record_down", "list_events", "buckets",
    "DEFAULT_LEDGER_PATH",
]
