"""Replay-detection ledger (MiniMax patch 4).

SQLite-backed ledger of seen (receipt_id, agent_id) pairs. A duplicate
receipt_id presented within the retention_lock_seconds window raises
ReplayDetected. After that window (but still inside retention_days) the
second presentation is allowed and silently bumps seen_count — the lock
window is the strict replay window; retention_days is how long the row
stays around for audit.

Thread-safe: a threading.Lock serializes the check-and-insert critical
section, and each DB operation opens its own short-lived sqlite3 connection
so the ledger is safe to share across threads on Windows (where the default
check_same_thread=True would otherwise bite).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReplayLedgerEntry:
    receipt_id: str
    agent_id: str
    first_seen_ts: float
    last_seen_ts: float
    seen_count: int
    source_ip: str | None


class ReplayDetected(Exception):
    """Raised when a receipt_id is presented a second time inside the lock window."""

    def __init__(self, receipt_id: str, first_seen: float):
        self.receipt_id = receipt_id
        self.first_seen = first_seen
        super().__init__(
            f"Replay detected: receipt_id '{receipt_id}' "
            f"first seen at {first_seen}, rejected now."
        )


class ReplayLedger:
    """See module docstring for semantics.

    retention_lock_seconds is the hard replay-rejection window.
    retention_days is how long rows stay in the DB for audit / prune.
    """

    def __init__(
        self,
        db_path: Path | str,
        retention_days: float = 30.0,
        retention_lock_seconds: float = 60.0,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.retention_lock_seconds = retention_lock_seconds
        self._lock = threading.Lock()
        self._init_db()
        self._prune_stale()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS receipt_ledger (
                    receipt_id  TEXT PRIMARY KEY,
                    agent_id    TEXT NOT NULL,
                    first_seen_ts REAL NOT NULL,
                    last_seen_ts  REAL NOT NULL,
                    seen_count    INTEGER DEFAULT 1,
                    source_ip      TEXT,
                    UNIQUE(receipt_id, agent_id)
                )
                """
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_receipt_ledger_ts
                ON receipt_ledger(last_seen_ts)
                """
            )

    def record(
        self,
        receipt_id: str,
        agent_id: str,
        source_ip: str | None = None,
    ) -> ReplayLedgerEntry:
        """Record a new receipt_id. Raises ReplayDetected on in-window duplicate."""
        with self._lock:
            now = time.time()
            cutoff = now - self.retention_lock_seconds

            existing = self._get_entry(receipt_id, agent_id)
            if existing and existing.last_seen_ts > cutoff:
                raise ReplayDetected(receipt_id, existing.first_seen_ts)

            entry = ReplayLedgerEntry(
                receipt_id=receipt_id,
                agent_id=agent_id,
                first_seen_ts=now,
                last_seen_ts=now,
                seen_count=1,
                source_ip=source_ip,
            )
            with sqlite3.connect(self.db_path) as con:
                con.execute(
                    """
                    INSERT INTO receipt_ledger
                        (receipt_id, agent_id, first_seen_ts, last_seen_ts, seen_count, source_ip)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(receipt_id, agent_id) DO UPDATE SET
                        last_seen_ts = excluded.last_seen_ts,
                        seen_count = receipt_ledger.seen_count + 1
                    """,
                    (receipt_id, agent_id, now, now, 1, source_ip),
                )
            return entry

    def seen(self, receipt_id: str, agent_id: str) -> bool:
        """Return True iff this (receipt_id, agent_id) is still inside retention_days."""
        entry = self._get_entry(receipt_id, agent_id)
        if entry is None:
            return False
        cutoff = time.time() - (self.retention_days * 86400)
        return entry.last_seen_ts > cutoff

    def get_history(self, agent_id: str, limit: int = 100) -> list[ReplayLedgerEntry]:
        """Return recent receipt history for an agent, newest first."""
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute(
                """
                SELECT receipt_id, agent_id, first_seen_ts, last_seen_ts, seen_count, source_ip
                FROM receipt_ledger
                WHERE agent_id = ?
                ORDER BY last_seen_ts DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [
            ReplayLedgerEntry(
                receipt_id=r[0],
                agent_id=r[1],
                first_seen_ts=r[2],
                last_seen_ts=r[3],
                seen_count=r[4],
                source_ip=r[5],
            )
            for r in rows
        ]

    def _get_entry(
        self, receipt_id: str, agent_id: str
    ) -> ReplayLedgerEntry | None:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                """
                SELECT receipt_id, agent_id, first_seen_ts, last_seen_ts, seen_count, source_ip
                FROM receipt_ledger
                WHERE receipt_id = ? AND agent_id = ?
                """,
                (receipt_id, agent_id),
            ).fetchone()
        if row is None:
            return None
        return ReplayLedgerEntry(
            receipt_id=row[0],
            agent_id=row[1],
            first_seen_ts=row[2],
            last_seen_ts=row[3],
            seen_count=row[4],
            source_ip=row[5],
        )

    def _prune_stale(self) -> int:
        cutoff = time.time() - (self.retention_days * 86400)
        with sqlite3.connect(self.db_path) as con:
            deleted = con.execute(
                "DELETE FROM receipt_ledger WHERE last_seen_ts < ?",
                (cutoff,),
            ).rowcount
        return deleted

    def prune(self) -> int:
        """Remove entries older than retention_days. Returns count pruned."""
        return self._prune_stale()

    def stats(self) -> dict:
        """Return ledger stats: total entries, per-agent breakdown, oldest ts."""
        with sqlite3.connect(self.db_path) as con:
            total = con.execute(
                "SELECT COUNT(*) FROM receipt_ledger"
            ).fetchone()[0]
            by_agent = dict(
                con.execute(
                    "SELECT agent_id, COUNT(*) FROM receipt_ledger GROUP BY agent_id"
                ).fetchall()
            )
            oldest = con.execute(
                "SELECT MIN(first_seen_ts) FROM receipt_ledger"
            ).fetchone()[0]
        return {
            "total_entries": total,
            "by_agent": by_agent,
            "oldest_entry_ts": oldest,
            "retention_days": self.retention_days,
        }
