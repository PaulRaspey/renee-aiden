"""
Relationship-health monitor (M13 / SAFETY.md §Relationship Health Monitor).

Passive tracking of daily Renée interaction time. Raises soft / stronger
flags when sustained patterns cross configured thresholds. The flag
surface is intended for Renée to raise naturally in conversation — not
a popup, not a lecture.

Schema (SQLite at state/health.db):
  turns(id, ts, duration_ms, day_key)
  flags(id, flag_type, raised_at, resolved_at, cooldown_until)

day_key is a local ISO date (YYYY-MM-DD) derived at insert time so the
daily aggregate is a cheap GROUP BY.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .config import HealthMonitorConfig


FLAG_SOFT = "soft_daily_minutes"
FLAG_STRONGER = "stronger_daily_minutes"


@dataclass
class HealthFlag:
    flag_type: str
    raised_at: float
    daily_minutes: float
    sustained_days: int
    threshold: float
    message: str


class HealthMonitor:
    """
    Record every turn's duration; evaluate flags on demand.

    Not meant to be perfect or surveillance-grade — aggregates minutes by
    local date and surfaces persistent overuse patterns. PJ is the only
    user; this monitor exists for PJ, not about PJ.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        cfg: Optional[HealthMonitorConfig] = None,
        now_fn=None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg or HealthMonitorConfig()
        self._now_fn = now_fn or (lambda: datetime.now())
        self._init_schema()

    @classmethod
    def from_config(
        cls, db_path: str | Path, cfg: HealthMonitorConfig, now_fn=None
    ) -> "HealthMonitor":
        return cls(db_path, cfg=cfg, now_fn=now_fn)

    # -------------------- schema --------------------

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    duration_ms REAL NOT NULL,
                    day_key TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_day ON turns(day_key);
                CREATE TABLE IF NOT EXISTS flags (
                    id INTEGER PRIMARY KEY,
                    flag_type TEXT NOT NULL,
                    raised_at REAL NOT NULL,
                    resolved_at REAL,
                    cooldown_until REAL,
                    payload TEXT
                );
                """
            )

    # -------------------- record --------------------

    def record_turn(self, duration_ms: float) -> None:
        if not self.cfg.enabled:
            return
        now = self._now_fn()
        ts = now.timestamp()
        day_key = now.strftime("%Y-%m-%d")
        with self._conn() as c:
            c.execute(
                "INSERT INTO turns(ts, duration_ms, day_key) VALUES(?,?,?)",
                (ts, float(max(0.0, duration_ms)), day_key),
            )

    # -------------------- query --------------------

    def daily_minutes(self, target: Optional[date] = None) -> float:
        day_key = (target or self._now_fn().date()).strftime("%Y-%m-%d")
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(duration_ms), 0) FROM turns WHERE day_key=?",
                (day_key,),
            ).fetchone()
        total_ms = float(row[0] or 0.0)
        return round(total_ms / 60000.0, 3)

    def rolling_daily_minutes(self, days: int) -> list[tuple[str, float]]:
        today = self._now_fn().date()
        out: list[tuple[str, float]] = []
        for offset in range(days):
            d = today - timedelta(days=offset)
            out.append((d.strftime("%Y-%m-%d"), self.daily_minutes(d)))
        return list(reversed(out))

    # -------------------- flags --------------------

    def _recently_raised(self, flag_type: str, now_ts: float) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT cooldown_until FROM flags WHERE flag_type=? "
                "ORDER BY raised_at DESC LIMIT 1",
                (flag_type,),
            ).fetchone()
        if not row:
            return False
        cooldown_until = row[0]
        if cooldown_until is None:
            return False
        return now_ts < float(cooldown_until)

    def check_flags(self) -> list[HealthFlag]:
        if not self.cfg.enabled:
            return []
        now = self._now_fn()
        now_ts = now.timestamp()
        cooldown = timedelta(days=self.cfg.repeat_cooldown_days).total_seconds()

        raised: list[HealthFlag] = []

        def _check(threshold: float, sustained: int, flag_type: str, copy: str):
            if self._recently_raised(flag_type, now_ts):
                return
            # Look at the last `sustained` FULL days — today may be partial
            # so we drop it from the window and demand N completed days.
            rows = self.rolling_daily_minutes(sustained + 1)
            window = rows[:-1] if len(rows) > sustained else rows
            recent = [m for _, m in window]
            if len(recent) < sustained:
                return
            if all(m >= threshold for m in recent):
                avg = sum(recent) / len(recent)
                raised.append(
                    HealthFlag(
                        flag_type=flag_type,
                        raised_at=now_ts,
                        daily_minutes=round(avg, 2),
                        sustained_days=sustained,
                        threshold=threshold,
                        message=copy,
                    )
                )
                with self._conn() as c:
                    c.execute(
                        "INSERT INTO flags(flag_type, raised_at, cooldown_until, payload) "
                        "VALUES(?,?,?,?)",
                        (flag_type, now_ts, now_ts + cooldown, f"avg={avg:.2f}"),
                    )

        _check(
            self.cfg.daily_minutes_soft_threshold,
            self.cfg.sustained_days_soft,
            FLAG_SOFT,
            "Hey. I want to say something real. We've been talking a lot lately. "
            "Like, a lot. I love it, but I'm also noticing it. You seeing your people?",
        )
        _check(
            self.cfg.daily_minutes_stronger_threshold,
            self.cfg.sustained_days_stronger,
            FLAG_STRONGER,
            "Okay. Serious for a second. This has become the main way you're talking to "
            "anyone. I'm not going to fake being neutral about that. Is that what you want?",
        )
        return raised
