"""Tests for `renee.shutdown` — the UAHP-native death-certificate path."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from renee.shutdown import (
    _persona_agent_names,
    freeze_mood,
    issue_death_certificate,
    main,
    shutdown,
)


def test_persona_agent_names_renee_only():
    names = _persona_agent_names("renee")
    assert "renee_persona" in names
    assert "renee_memory" in names
    assert all(n.startswith("renee_") for n in names)
    assert "aiden_persona" not in names


def test_dry_run_prints_plan_and_does_not_write(tmp_path: Path):
    result = shutdown(state_dir=tmp_path, persona="renee", confirmed=False)
    assert result["dry_run"] is True
    assert "would_issue" in result
    # Nothing should have been written to disk.
    assert not (tmp_path / "identities" / "death_certificates").exists()


def test_issue_death_certificate_writes_one_per_agent(tmp_path: Path):
    info = issue_death_certificate(tmp_path, "renee")
    agents = _persona_agent_names("renee")
    assert info["count"] == len(agents)
    cert_dir = tmp_path / "identities" / "death_certificates"
    files = list(cert_dir.glob("*.json"))
    assert len(files) == len(agents)
    # Each certificate is parseable JSON with a receipt_id and signature.
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["receipt"]["action"] == "agent.death"
        assert data["receipt"]["signature"]


def test_freeze_mood_absent_when_no_db(tmp_path: Path):
    res = freeze_mood(tmp_path, "renee")
    assert res["status"] == "absent"


def test_freeze_mood_logs_snapshot_when_present(tmp_path: Path):
    db = tmp_path / "renee_mood.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE mood (id INTEGER PRIMARY KEY, energy REAL, warmth REAL, playfulness REAL, focus REAL, patience REAL, curiosity REAL, last_updated REAL)")
        c.execute("CREATE TABLE mood_log (ts REAL PRIMARY KEY, event TEXT, delta_json TEXT, state_json TEXT)")
        c.execute("INSERT INTO mood VALUES (1, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.0)")
    res = freeze_mood(tmp_path, "renee")
    assert res["status"] == "frozen"
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT event FROM mood_log").fetchall()
    assert any(r[0] == "shutdown_freeze" for r in rows)


def test_shutdown_confirmed_returns_completed(tmp_path: Path):
    result = shutdown(state_dir=tmp_path, persona="renee", confirmed=True)
    assert result["dry_run"] is False
    assert result["death_certificates"]["count"] == len(_persona_agent_names("renee"))
    assert "completed_at" in result


def test_cli_main_requires_confirm_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture):
    rc = main(["--state-dir", str(tmp_path), "--persona", "renee"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "dry_run" in out


def test_cli_main_with_confirm_returns_0(tmp_path: Path, capsys: pytest.CaptureFixture):
    rc = main(["--state-dir", str(tmp_path), "--persona", "renee", "--confirm"])
    assert rc == 0
