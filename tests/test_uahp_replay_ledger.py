"""Tests for the replay-detection ledger (patch 4)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.uahp import replay_ledger as rl_mod
from src.uahp.replay_ledger import ReplayDetected, ReplayLedger


def test_record_fresh_receipt_succeeds(tmp_path: Path):
    ledger = ReplayLedger(tmp_path / "rl.db", retention_lock_seconds=60.0)
    entry = ledger.record("receipt-abc", "renee_voice")
    assert entry.receipt_id == "receipt-abc"
    assert entry.agent_id == "renee_voice"
    assert entry.seen_count == 1
    assert ledger.seen("receipt-abc", "renee_voice") is True


def test_duplicate_within_retention_lock_raises(tmp_path: Path):
    ledger = ReplayLedger(tmp_path / "rl.db", retention_lock_seconds=60.0)
    ledger.record("r1", "alice")
    with pytest.raises(ReplayDetected) as exc_info:
        ledger.record("r1", "alice")
    assert exc_info.value.receipt_id == "r1"


def test_duplicate_after_retention_lock_allowed(tmp_path: Path):
    """retention_lock_seconds=0 lets a second presentation through; it
    silently bumps seen_count instead of raising. Confirms the intentional
    semantics of patch 4: lock window is strict-reject, retention_days is
    just audit retention."""
    ledger = ReplayLedger(
        tmp_path / "rl.db", retention_days=30.0, retention_lock_seconds=0.0
    )
    ledger.record("r1", "alice")
    # With a zero lock window, the second record falls through to the
    # ON CONFLICT DO UPDATE branch rather than raising.
    ledger.record("r1", "alice")
    assert ledger.stats()["total_entries"] == 1
    history = ledger.get_history("alice")
    assert len(history) == 1
    assert history[0].seen_count == 2


def test_concurrent_distinct_receipts_all_succeed(tmp_path: Path):
    ledger = ReplayLedger(tmp_path / "rl.db", retention_lock_seconds=60.0)
    errors: list[BaseException] = []

    def _record(i: int) -> None:
        try:
            ledger.record(f"receipt-{i}", "agent")
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=_record, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert ledger.stats()["total_entries"] == 10
    assert ledger.stats()["by_agent"] == {"agent": 10}


def test_concurrent_same_receipt_exactly_one_succeeds(tmp_path: Path):
    ledger = ReplayLedger(tmp_path / "rl.db", retention_lock_seconds=60.0)
    successes: list[int] = []
    rejections: list[ReplayDetected] = []

    def _record(i: int) -> None:
        try:
            ledger.record("receipt-same", "agent")
            successes.append(i)
        except ReplayDetected as e:
            rejections.append(e)

    threads = [threading.Thread(target=_record, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(successes) == 1
    assert len(rejections) == 9


def test_prune_removes_entries_past_retention_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ledger = ReplayLedger(
        tmp_path / "rl.db", retention_days=30.0, retention_lock_seconds=0.0
    )
    ledger.record("r1", "alice")
    ledger.record("r2", "alice")
    ledger.record("r3", "bob")
    assert ledger.stats()["total_entries"] == 3

    real_time = rl_mod.time.time
    monkeypatch.setattr(
        rl_mod.time, "time", lambda: real_time() + 31 * 86400
    )
    pruned = ledger.prune()
    assert pruned == 3
    assert ledger.stats()["total_entries"] == 0


def test_stats_reports_total_and_per_agent(tmp_path: Path):
    ledger = ReplayLedger(tmp_path / "rl.db", retention_lock_seconds=60.0)
    ledger.record("r1", "alice")
    ledger.record("r2", "alice")
    ledger.record("r3", "alice")
    ledger.record("r4", "bob")
    s = ledger.stats()
    assert s["total_entries"] == 4
    assert s["by_agent"] == {"alice": 3, "bob": 1}
    assert s["retention_days"] == 30.0
    assert s["oldest_entry_ts"] is not None


def test_ledger_survives_restart(tmp_path: Path):
    db = tmp_path / "rl.db"
    ledger = ReplayLedger(db, retention_lock_seconds=60.0)
    ledger.record("r1", "alice")
    del ledger
    ledger2 = ReplayLedger(db, retention_lock_seconds=60.0)
    assert ledger2.seen("r1", "alice") is True
    with pytest.raises(ReplayDetected):
        ledger2.record("r1", "alice")
