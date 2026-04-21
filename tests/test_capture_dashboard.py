"""Tests for the dashboard Sessions tab + backend helpers.

Exercises src/capture/dashboard_sessions.py directly for the pure
aggregation logic, then spins up the FastAPI app via TestClient to
verify the routes are wired, the presence-score lock works, notes
persist, and tab switching does not regress.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.capture import dashboard_sessions as sessions_mod
from src.capture.dashboard_sessions import PresenceScoreLockedError
from src.dashboard.config import DashboardConfig
from src.dashboard.server import build_app


ROOT = Path(__file__).resolve().parents[1]


def _write_wav(path: Path, seconds: float = 1.0) -> None:
    n = int(48000 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(bytes(n * 2))


def _make_session(
    sessions_root: Path,
    session_id: str,
    *,
    presence_score=None,
    public: bool = False,
    github_published: bool = False,
    flags: list | None = None,
    latency: dict | None = None,
    start_time: str = "2026-04-21T19:30:00+00:00",
    end_time: str = "2026-04-21T19:45:00+00:00",
    backend_used: str = "cascade",
    eval_scores: list | None = None,
    notes: str | None = None,
) -> Path:
    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True)
    _write_wav(session_dir / "mic.wav")
    _write_wav(session_dir / "renee.wav")
    manifest = {
        "session_id": session_id,
        "start_time": start_time,
        "end_time": end_time,
        "renee_versions": {"persona": "0.5.5"},
        "backend_used": backend_used,
        "pod_id": None,
        "starter_metadata": {},
        "public": public,
        "reviewed": False,
        "github_published": github_published,
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
    (session_dir / "latency.json").write_text(
        json.dumps(latency or {"count": 0, "p50_s": 0, "p95_s": 0, "p99_s": 0}),
        encoding="utf-8",
    )
    (session_dir / "eval_scores.json").write_text(
        json.dumps(eval_scores or []), encoding="utf-8",
    )
    (session_dir / "transcript.json").write_text("[]", encoding="utf-8")
    if notes is not None:
        (session_dir / "notes.md").write_text(notes, encoding="utf-8")
    return session_dir


# ---------------------------------------------------------------------------
# pure helper tests
# ---------------------------------------------------------------------------


def test_list_sessions_empty_when_root_missing(tmp_path):
    assert sessions_mod.list_sessions(tmp_path / "does-not-exist") == []


def test_list_sessions_returns_sorted_and_meta(tmp_path):
    _make_session(
        tmp_path, "2026-04-21T19-00-00", presence_score=4, flags=[{"category": "long_pause", "severity": "medium", "description": "", "timestamp": 1.0, "source": {}}],
    )
    _make_session(tmp_path, "2026-04-22T08-00-00", presence_score=None)
    listed = sessions_mod.list_sessions(tmp_path)
    assert [s["session_id"] for s in listed] == [
        "2026-04-22T08-00-00",
        "2026-04-21T19-00-00",
    ]
    assert listed[1]["flag_count"] == 1
    assert listed[1]["presence_score"] == 4


def test_session_detail_includes_flags_and_urls(tmp_path):
    _make_session(
        tmp_path, "s1",
        flags=[{"category": "long_pause", "severity": "medium",
                "description": "x", "timestamp": 2.5, "source": {}}],
    )
    d = sessions_mod.session_detail(tmp_path, "s1")
    assert d["manifest"]["session_id"] == "s1"
    assert len(d["flags"]) == 1
    assert d["mic_wav_url"] == "/api/sessions/s1/audio/mic.wav"
    assert d["renee_wav_url"] == "/api/sessions/s1/audio/renee.wav"


def test_session_detail_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        sessions_mod.session_detail(tmp_path, "nope")


def test_resolve_session_audio_rejects_bad_names(tmp_path):
    _make_session(tmp_path, "s1")
    with pytest.raises(ValueError):
        sessions_mod.resolve_session_audio(tmp_path, "s1", "secret.wav")


def test_resolve_session_audio_resolves_valid_names(tmp_path):
    _make_session(tmp_path, "s1")
    p = sessions_mod.resolve_session_audio(tmp_path, "s1", "mic.wav")
    assert p.exists()
    assert p.name == "mic.wav"


def test_session_trends_empty_dir(tmp_path):
    assert sessions_mod.session_trends(tmp_path)["count"] == 0


def test_session_trends_aggregates(tmp_path):
    _make_session(
        tmp_path, "s1",
        flags=[
            {"category": "long_pause", "severity": "medium", "description": "", "timestamp": 1.0, "source": {}},
            {"category": "safety_trigger", "severity": "low", "description": "", "timestamp": 2.0, "source": {}},
            {"category": "overlap", "severity": "high", "description": "", "timestamp": 3.0, "source": {}},
        ],
        latency={"count": 3, "p50_s": 0.8, "p95_s": 1.4, "p99_s": 1.9},
    )
    trends = sessions_mod.session_trends(tmp_path)
    s = trends["sessions"][0]
    assert s["flag_total"] == 3
    assert s["flag_categories"]["safety_trigger"] == 1
    assert s["flag_categories"]["overlap"] == 1
    assert s["safety_count"] == 1
    assert s["overlap_count"] == 1
    assert s["latency_p95_s"] == pytest.approx(1.4)


def test_disk_usage_empty_directory(tmp_path):
    root = tmp_path / "sessions"
    du = sessions_mod.disk_usage(root)
    assert du["session_count"] == 0
    assert du["sessions_total_bytes"] == 0


def test_disk_usage_computes_sizes(tmp_path):
    _make_session(tmp_path, "s1")
    _make_session(tmp_path, "s2")
    du = sessions_mod.disk_usage(tmp_path)
    assert du["session_count"] == 2
    assert du["sessions_total_bytes"] > 0
    assert du["free_bytes"] >= 0


# ---------------------------------------------------------------------------
# presence score
# ---------------------------------------------------------------------------


def test_set_presence_score_happy_path(tmp_path):
    _make_session(tmp_path, "s1")
    manifest = sessions_mod.set_presence_score(tmp_path, "s1", 3)
    assert manifest["presence_score"] == 3


def test_set_presence_score_rejects_out_of_range(tmp_path):
    _make_session(tmp_path, "s1")
    with pytest.raises(ValueError):
        sessions_mod.set_presence_score(tmp_path, "s1", 0)
    with pytest.raises(ValueError):
        sessions_mod.set_presence_score(tmp_path, "s1", 6)


def test_set_presence_score_rejects_non_int(tmp_path):
    _make_session(tmp_path, "s1")
    with pytest.raises(ValueError):
        sessions_mod.set_presence_score(tmp_path, "s1", 2.5)  # type: ignore[arg-type]


def test_set_presence_score_missing_session_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        sessions_mod.set_presence_score(tmp_path, "ghost", 3)


def test_set_presence_score_locked_after_publish(tmp_path):
    _make_session(tmp_path, "s1", github_published=True, presence_score=4)
    with pytest.raises(PresenceScoreLockedError):
        sessions_mod.set_presence_score(tmp_path, "s1", 5)


# ---------------------------------------------------------------------------
# FastAPI app
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
        yaml.safe_dump({"reality_anchors": {"enabled": True}, "health_monitor": {}, "bad_day": {}}),
        encoding="utf-8",
    )
    (config_dir / "voice.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")
    cfg = DashboardConfig(
        bind_host="127.0.0.1",
        port=7860,
        password="",
        state_dir=str(state_dir),
        config_dir=str(config_dir),
        persona="renee",
        sessions_root=str(sessions_root),
    )
    app = build_app(cfg)
    client = TestClient(app)
    client.sessions_root = sessions_root  # type: ignore[attr-defined]
    return client


def test_sessions_list_empty(client):
    r = client.get("/api/sessions/list")
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


def test_sessions_list_returns_seeded(client):
    _make_session(client.sessions_root, "s1", presence_score=3)
    r = client.get("/api/sessions/list")
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == "s1"


def test_sessions_trends_empty(client):
    r = client.get("/api/sessions/trends")
    assert r.status_code == 200
    assert r.json() == {"sessions": [], "count": 0}


def test_sessions_disk_usage_zero(client):
    r = client.get("/api/sessions/disk_usage")
    assert r.status_code == 200
    body = r.json()
    assert body["session_count"] == 0


def test_sessions_detail_404_on_missing(client):
    r = client.get("/api/sessions/ghost/detail")
    assert r.status_code == 404


def test_sessions_detail_returns_payload(client):
    _make_session(client.sessions_root, "s1")
    r = client.get("/api/sessions/s1/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"]["session_id"] == "s1"


def test_sessions_audio_streams_wav(client):
    _make_session(client.sessions_root, "s1")
    r = client.get("/api/sessions/s1/audio/mic.wav")
    assert r.status_code == 200
    assert "audio/wav" in r.headers["content-type"]
    assert r.content.startswith(b"RIFF")


def test_sessions_audio_rejects_bad_name(client):
    _make_session(client.sessions_root, "s1")
    r = client.get("/api/sessions/s1/audio/secret.wav")
    assert r.status_code == 400


def test_presence_score_post_accepts_valid(client):
    _make_session(client.sessions_root, "s1")
    r = client.post("/api/sessions/s1/presence_score", json={"score": 4})
    assert r.status_code == 200
    assert r.json()["presence_score"] == 4


def test_presence_score_post_rejects_out_of_range(client):
    _make_session(client.sessions_root, "s1")
    r = client.post("/api/sessions/s1/presence_score", json={"score": 7})
    assert r.status_code == 422


def test_presence_score_post_locked_after_publish(client):
    _make_session(client.sessions_root, "s1", github_published=True, presence_score=4)
    r = client.post("/api/sessions/s1/presence_score", json={"score": 5})
    assert r.status_code == 409


def test_sessions_notes_round_trip(client):
    _make_session(client.sessions_root, "s1")
    r = client.post("/api/sessions/s1/notes", json={"notes": "first note"})
    assert r.status_code == 200
    assert (client.sessions_root / "s1" / "notes.md").read_text(encoding="utf-8") == "first note"


def test_index_includes_sessions_tab_button(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'data-tab="sessions"' in body
    assert 'id="tab-sessions"' in body
