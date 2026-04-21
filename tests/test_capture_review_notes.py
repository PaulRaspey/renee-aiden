"""Tests for the review notes surface (Feature 5).

Covers notes.md template creation, disk round-trips, tag extraction,
and HIGHLIGHTS.md + HIGHLIGHTS_PRIVATE.md regeneration including the
public / private partition.
"""
from __future__ import annotations

import datetime as _dt
import json
import wave
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.capture import review_notes
from src.capture.review_notes import (
    DEFAULT_TAGS,
    NotesBlock,
    collect_tagged_blocks,
    ensure_notes_exists,
    find_tags,
    initial_notes_content,
    parse_blocks,
    read_notes,
    regenerate_highlights,
    save_notes,
)
from src.cli.main import main as cli_main
from src.dashboard.config import DashboardConfig
from src.dashboard.server import build_app


ROOT = Path(__file__).resolve().parents[1]


def _write_wav(path: Path, seconds: float = 1.0) -> None:
    n = int(48000 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(bytes(n * 2))


def _make_session(
    sessions_root: Path,
    session_id: str,
    *,
    public: bool = False,
    presence_score=None,
    notes: str | None = None,
    flags: list | None = None,
    backend_used: str = "cascade",
    start_time: str = "2026-04-21T19:30:00+00:00",
    end_time: str = "2026-04-21T19:45:00+00:00",
) -> Path:
    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True)
    _write_wav(session_dir / "mic.wav")
    _write_wav(session_dir / "renee.wav")
    manifest = {
        "session_id": session_id,
        "start_time": start_time,
        "end_time": end_time,
        "renee_versions": {},
        "backend_used": backend_used,
        "pod_id": None,
        "starter_metadata": {
            "starter_index": 3,
            "curveball_planned_minute": 18,
            "curveball_actual_minute": 17,
        },
        "public": public,
        "reviewed": False,
        "github_published": False,
        "presence_score": presence_score,
        "notes_file": str(session_dir / "notes.md"),
        "genesis_session": False,
        "memory_snapshot": {},
    }
    (session_dir / "session_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    (session_dir / "flags.json").write_text(
        json.dumps(flags or []), encoding="utf-8",
    )
    (session_dir / "transcript.json").write_text("[]", encoding="utf-8")
    (session_dir / "eval_scores.json").write_text("[]", encoding="utf-8")
    (session_dir / "latency.json").write_text(
        json.dumps({"count": 0, "p50_s": 0, "p95_s": 0, "p99_s": 0}),
        encoding="utf-8",
    )
    if notes is not None:
        (session_dir / "notes.md").write_text(notes, encoding="utf-8")
    return session_dir


# ---------------------------------------------------------------------------
# initial template
# ---------------------------------------------------------------------------


def test_initial_notes_includes_overview_and_flags(tmp_path):
    manifest = {
        "session_id": "s1",
        "start_time": "2026-04-21T19:30:00+00:00",
        "end_time": "2026-04-21T20:22:00+00:00",
        "backend_used": "cascade",
        "starter_metadata": {
            "starter_index": 3,
            "curveball_planned_minute": 18,
            "curveball_actual_minute": 17,
        },
    }
    flags = [
        {
            "timestamp": 202.0,
            "category": "long_pause",
            "severity": "medium",
            "description": "Renee paused 2.4s before the Alps",
            "source": {},
        }
    ]
    content = initial_notes_content(manifest, flags)
    assert "# Session s1" in content
    assert "Duration: 52:00" in content
    assert "Backend: cascade" in content
    assert "[00:03:22] long_pause, medium" in content
    assert "Renee paused 2.4s before the Alps" in content
    assert "PJ notes:" in content


def test_initial_notes_no_flags_still_valid():
    manifest = {"session_id": "s2", "start_time": "", "end_time": "",
                "backend_used": "cascade", "starter_metadata": {}}
    content = initial_notes_content(manifest, [])
    assert "## Flags" in content
    assert "no flags surfaced" in content.lower()


def test_ensure_notes_exists_creates_on_first_call(tmp_path):
    session_dir = _make_session(tmp_path, "s1")
    assert not (session_dir / "notes.md").exists()
    notes_path = ensure_notes_exists(session_dir)
    assert notes_path.exists()
    first_content = notes_path.read_text(encoding="utf-8")
    # Second call must not overwrite.
    save_notes(session_dir, first_content + "\n\nPJ: added line")
    notes_path = ensure_notes_exists(session_dir)
    assert "added line" in notes_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# tag extraction
# ---------------------------------------------------------------------------


def test_find_tags_basic():
    assert find_tags("keep this #harvest moment #fix").count("harvest") == 1
    assert set(find_tags("one #harvest two #fix")) == {"harvest", "fix"}


def test_find_tags_ignores_hash_in_code_or_ids():
    assert find_tags("issue #12 is #12345") == []
    assert find_tags("s1#harvest suffix") == []


def test_parse_blocks_level_2_and_3():
    notes = (
        "# Session s1\n\n"
        "## Overview\n- line\n\n"
        "## Flags\n\n"
        "### [00:00:10] long_pause, medium\nbody text\n\n"
        "### [00:00:30] overlap, low\nother body\n"
    )
    blocks = parse_blocks(notes)
    headings = [b.heading for b in blocks]
    assert "Overview" in headings
    assert "Flags" in headings
    flag_blocks = [b for b in blocks if b.level == 3]
    assert len(flag_blocks) == 2


# ---------------------------------------------------------------------------
# highlights
# ---------------------------------------------------------------------------


def test_regenerate_highlights_empty_dir_writes_shells(tmp_path):
    root = tmp_path / "renee-sessions"
    result = regenerate_highlights(root)
    assert result["public_block_count"] == 0
    assert result["private_block_count"] == 0
    assert (root / "HIGHLIGHTS.md").exists()
    assert (root / "HIGHLIGHTS_PRIVATE.md").exists()


def test_regenerate_highlights_respects_public_flag(tmp_path):
    root = tmp_path / "renee-sessions"
    public_notes = (
        "# Session s1\n\n"
        "## Flags\n\n"
        "### [00:03:22] long_pause\nRenee paused. #harvest worth keeping\nPJ notes:\n"
    )
    private_notes = (
        "# Session s2\n\n"
        "## Flags\n\n"
        "### [00:05:10] overlap\nWe talked over each other. #fix\nPJ notes:\n"
    )
    _make_session(root, "s1", public=True, notes=public_notes,
                  start_time="2026-04-21T19:30:00+00:00",
                  end_time="2026-04-21T19:32:00+00:00")
    _make_session(root, "s2", public=False, notes=private_notes,
                  start_time="2026-04-22T19:30:00+00:00",
                  end_time="2026-04-22T19:32:00+00:00")
    result = regenerate_highlights(root)
    assert result["public_block_count"] == 1
    assert result["private_block_count"] == 2
    public_md = (root / "HIGHLIGHTS.md").read_text(encoding="utf-8")
    private_md = (root / "HIGHLIGHTS_PRIVATE.md").read_text(encoding="utf-8")
    assert "Renee paused" in public_md
    assert "We talked over each other" not in public_md
    assert "Renee paused" in private_md
    assert "We talked over each other" in private_md


def test_regenerate_highlights_groups_by_tag(tmp_path):
    root = tmp_path / "renee-sessions"
    notes = (
        "# Session s1\n\n## Flags\n\n"
        "### [00:00:10] overlap\nfix this pattern #fix\nPJ notes:\n\n"
        "### [00:00:30] long_pause\nkeep this one #harvest #moment\nPJ notes:\n"
    )
    _make_session(root, "s1", public=True, notes=notes)
    regenerate_highlights(root)
    md = (root / "HIGHLIGHTS.md").read_text(encoding="utf-8")
    assert "## #harvest" in md
    assert "## #fix" in md
    assert "## #moment" in md
    harvest_idx = md.index("## #harvest")
    fix_idx = md.index("## #fix")
    moment_idx = md.index("## #moment")
    # Tag order is stable: harvest, fix, moment
    assert harvest_idx < fix_idx < moment_idx


def test_collect_tagged_blocks_skips_untagged_blocks(tmp_path):
    root = tmp_path / "renee-sessions"
    notes = (
        "# Session s1\n\n## Flags\n\n"
        "### [00:00:10] a\nno tag here\nPJ notes:\n\n"
        "### [00:00:30] b\nonly this one #harvest\nPJ notes:\n"
    )
    _make_session(root, "s1", public=True, notes=notes)
    blocks = collect_tagged_blocks(root)
    assert len(blocks) == 1
    assert "harvest" in blocks[0].tags


# ---------------------------------------------------------------------------
# direct-disk-edit reflects on reload
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    sessions_root = tmp_path / "renee-sessions"
    sessions_root.mkdir()
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "configs"
    state_dir.mkdir()
    config_dir.mkdir()
    renee_yaml = ROOT / "configs" / "renee.yaml"
    (config_dir / "renee.yaml").write_text(
        renee_yaml.read_text(encoding="utf-8"), encoding="utf-8",
    )
    (config_dir / "safety.yaml").write_text(
        yaml.safe_dump({"reality_anchors": {}, "health_monitor": {}, "bad_day": {}}),
        encoding="utf-8",
    )
    (config_dir / "voice.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")
    cfg = DashboardConfig(
        bind_host="127.0.0.1", port=7860, password="",
        state_dir=str(state_dir), config_dir=str(config_dir),
        persona="renee", sessions_root=str(sessions_root),
    )
    app = build_app(cfg)
    client = TestClient(app)
    client.sessions_root = sessions_root
    return client


def test_dashboard_detail_creates_notes_if_missing(client):
    _make_session(client.sessions_root, "s1")
    assert not (client.sessions_root / "s1" / "notes.md").exists()
    r = client.get("/api/sessions/s1/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["notes"]
    assert (client.sessions_root / "s1" / "notes.md").exists()


def test_direct_disk_edit_reflected_on_next_detail_load(client):
    _make_session(client.sessions_root, "s1")
    # First load creates the initial template.
    client.get("/api/sessions/s1/detail")
    disk_path = client.sessions_root / "s1" / "notes.md"
    disk_path.write_text("# hand-edited\n\nunique-marker-42\n", encoding="utf-8")
    r = client.get("/api/sessions/s1/detail")
    assert r.status_code == 200
    assert "unique-marker-42" in r.json()["notes"]


def test_notes_round_trip_via_dashboard(client):
    _make_session(client.sessions_root, "s1")
    r = client.post(
        "/api/sessions/s1/notes", json={"notes": "# saved\ncontent here\n"},
    )
    assert r.status_code == 200
    r2 = client.get("/api/sessions/s1/detail")
    assert "content here" in r2.json()["notes"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_highlights_cli_runs(tmp_path, capsys, monkeypatch):
    root = tmp_path / "renee-sessions"
    root.mkdir()
    _make_session(
        root, "s1", public=True,
        notes="# s1\n\n## Flags\n\n### [00:00:10] x\nkeep #harvest\nPJ notes:\n",
    )
    rc = cli_main(["highlights", "--sessions-root", str(root)])
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["public_block_count"] == 1
    assert (root / "HIGHLIGHTS.md").exists()
