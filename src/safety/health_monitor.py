"""
Relationship-health monitor (M13 / SAFETY.md §Relationship Health Monitor).

Passive tracking of daily Renée interaction time. Raises soft / stronger
flags when sustained patterns cross configured thresholds. The flag
surface is intended for Renée to raise naturally in conversation — not
a popup, not a lecture.

Schema (SQLite at state/health.db):
  turns(id, ts, duration_ms, day_key)
  flags(id, flag_type, raised_at, resolved_at, cooldown_until)
  bridge_cooldowns(id, triggered_at, cooldown_until, reason)

day_key is a local ISO date (YYYY-MM-DD) derived at insert time so the
daily aggregate is a cheap GROUP BY. bridge_cooldowns carries hard-stop
events; the most recent row with `cooldown_until > now` means the bridge
is offline by policy.
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
CAP_REASON_DAILY = "daily_cap_exceeded"


@dataclass
class HealthFlag:
    flag_type: str
    raised_at: float
    daily_minutes: float
    sustained_days: int
    threshold: float
    message: str


@dataclass
class CapOutcome:
    """Result of evaluating the hard daily cap after a turn.

    `just_tripped` is True only on the specific turn that pushed the day's
    total across the cap. `already_tripped` means the cap was crossed on a
    prior turn today and the bridge is still in cooldown; the caller should
    still end the current turn but not re-trigger the farewell.
    `minutes_used` and `minutes_cap` are in minutes for easy comparison.
    """
    just_tripped: bool = False
    already_tripped: bool = False
    minutes_used: float = 0.0
    minutes_cap: float = 0.0
    cooldown_until: Optional[float] = None
    farewell: str = ""

    @property
    def tripped(self) -> bool:
        return self.just_tripped or self.already_tripped


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
                CREATE TABLE IF NOT EXISTS bridge_cooldowns (
                    id INTEGER PRIMARY KEY,
                    triggered_at REAL NOT NULL,
                    cooldown_until REAL NOT NULL,
                    day_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    minutes_used REAL NOT NULL,
                    minutes_cap REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bridge_cooldowns_day
                    ON bridge_cooldowns(day_key);
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

    def daily_summary(self) -> float:
        """In-session convenience: minutes logged so far for the current
        local day. Unlike `check_flags()`, this includes the partial
        today window so the caller can see usage without waiting for a
        completed day."""
        return self.daily_minutes()

    def rolling_daily_minutes(self, days: int) -> list[tuple[str, float]]:
        today = self._now_fn().date()
        out: list[tuple[str, float]] = []
        for offset in range(days):
            d = today - timedelta(days=offset)
            out.append((d.strftime("%Y-%m-%d"), self.daily_minutes(d)))
        return list(reversed(out))

    def rolling_average_minutes(self, days: int) -> float:
        """Mean daily minutes over the trailing `days`-day window, including
        today. Returns 0.0 if the window has no recorded turns."""
        if days <= 0:
            return 0.0
        rows = self.rolling_daily_minutes(days)
        minutes = [m for _, m in rows]
        if not minutes:
            return 0.0
        return round(sum(minutes) / len(minutes), 3)

    def seven_day_average_minutes(self) -> float:
        return self.rolling_average_minutes(7)

    def thirty_day_average_minutes(self) -> float:
        return self.rolling_average_minutes(30)

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

    # -------------------- hard daily cap --------------------

    def evaluate_cap(self) -> CapOutcome:
        """Evaluate the hard daily cap given the current aggregate usage.

        Always safe to call. Returns an outcome that describes whether the
        bridge should keep the session alive, end it now, or stay offline.
        Idempotent across calls on the same day once tripped: `just_tripped`
        flips True exactly once, and a new row lands in bridge_cooldowns.
        """
        outcome = CapOutcome(
            minutes_cap=float(self.cfg.daily_cap_minutes or 0),
            farewell=self.cfg.cap_disconnect_message,
        )
        if not self.cfg.enabled:
            return outcome
        cap = self.cfg.daily_cap_minutes or 0
        if cap <= 0:
            return outcome
        used = self.daily_minutes()
        outcome.minutes_used = used
        if used < cap:
            # Still under the cap; clear any lingering "already tripped"
            # record check: the bridge cooldown may still apply if a trip
            # happened on a prior part of the same day and we're reading
            # `already_tripped` out of SQLite.
            existing = self._bridge_cooldown_for_today()
            if existing is not None:
                outcome.already_tripped = True
                outcome.cooldown_until = existing
            return outcome
        # At or over the cap. Record a new cooldown row iff one isn't
        # already active for today.
        existing = self._bridge_cooldown_for_today()
        if existing is not None:
            outcome.already_tripped = True
            outcome.cooldown_until = existing
            return outcome
        now = self._now_fn().timestamp()
        cooldown_until = now + float(self.cfg.post_cap_cooldown_minutes or 0) * 60.0
        day_key = self._now_fn().strftime("%Y-%m-%d")
        with self._conn() as c:
            c.execute(
                "INSERT INTO bridge_cooldowns "
                "(triggered_at, cooldown_until, day_key, reason, minutes_used, minutes_cap) "
                "VALUES (?,?,?,?,?,?)",
                (now, cooldown_until, day_key, CAP_REASON_DAILY, used, float(cap)),
            )
        outcome.just_tripped = True
        outcome.cooldown_until = cooldown_until
        return outcome

    def bridge_allowed_now(self) -> bool:
        """Is the audio bridge currently allowed to accept connections?

        False when a bridge cooldown row is still in the future, True
        otherwise.
        """
        if not self.cfg.enabled:
            return True
        cap = self.cfg.daily_cap_minutes or 0
        if cap <= 0:
            return True
        existing = self._active_bridge_cooldown_until()
        return existing is None

    def bridge_cooldown_until(self) -> Optional[float]:
        """Absolute timestamp (epoch seconds) until which the bridge is
        offline by policy. None when the bridge is online or the cap is
        disabled."""
        return self._active_bridge_cooldown_until()

    def latest_bridge_cooldown(self) -> Optional[dict]:
        """Return the most recent bridge cooldown record as a dict, or None
        if none exist. Used by the dashboard."""
        with self._conn() as c:
            row = c.execute(
                "SELECT triggered_at, cooldown_until, day_key, reason, "
                "minutes_used, minutes_cap "
                "FROM bridge_cooldowns ORDER BY triggered_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {
            "triggered_at": float(row[0]),
            "cooldown_until": float(row[1]),
            "day_key": str(row[2]),
            "reason": str(row[3]),
            "minutes_used": float(row[4]),
            "minutes_cap": float(row[5]),
        }

    def _active_bridge_cooldown_until(self) -> Optional[float]:
        now_ts = self._now_fn().timestamp()
        with self._conn() as c:
            row = c.execute(
                "SELECT cooldown_until FROM bridge_cooldowns "
                "WHERE cooldown_until > ? ORDER BY cooldown_until DESC LIMIT 1",
                (now_ts,),
            ).fetchone()
        if not row:
            return None
        return float(row[0])

    def _bridge_cooldown_for_today(self) -> Optional[float]:
        """Return the cooldown_until (epoch seconds) if a cooldown was
        triggered today AND is still in the future. Used to decide whether
        to mark `already_tripped` on a fresh CapOutcome."""
        now = self._now_fn()
        day_key = now.strftime("%Y-%m-%d")
        now_ts = now.timestamp()
        with self._conn() as c:
            row = c.execute(
                "SELECT cooldown_until FROM bridge_cooldowns "
                "WHERE day_key=? AND cooldown_until > ? "
                "ORDER BY triggered_at DESC LIMIT 1",
                (day_key, now_ts),
            ).fetchone()
        if not row:
            return None
        return float(row[0])
