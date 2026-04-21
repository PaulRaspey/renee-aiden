"""Tests for the one-command recording startup flow (Feature 7).

Exercises src.capture.record_runner.run_recording_session with fake
implementations for every side effect: pod status, dashboard ping,
browser open, bridge start, triage trigger, and the Ctrl+C wait. No
real subprocesses spawn, no sockets open.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path
from typing import Any

import pytest

from src.capture import record_runner
from src.capture.record_runner import POD_COLD_MESSAGE, run_recording_session


class _FakeBridge:
    def __init__(self):
        self.started = True
        self.waited = False
        self.terminated = False

    def wait(self):
        self.waited = True

    def terminate(self):
        self.terminated = True


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(bytes(48000 * 2))


def _make_session(sessions_root: Path, session_id: str) -> Path:
    sessions_root.mkdir(parents=True, exist_ok=True)
    d = sessions_root / session_id
    d.mkdir()
    _write_wav(d / "mic.wav")
    _write_wav(d / "renee.wav")
    (d / "session_manifest.json").write_text(
        json.dumps({"session_id": session_id, "start_time": "", "end_time": ""}),
        encoding="utf-8",
    )
    return d


def _collector():
    items = []
    return items, lambda msg: items.append(msg)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_happy_path_starts_bridge_and_triages(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    session_dir = _make_session(sessions_root, "s1")
    triage_calls = []
    started = []
    msgs, indicator = _collector()
    bridge = _FakeBridge()

    rc = run_recording_session(
        pod_reachable_fn=lambda: (True, "RUNNING"),
        dashboard_running_fn=lambda: False,
        start_dashboard_fn=lambda: started.append("dash"),
        open_browser_fn=lambda url: started.append(("browser", url)),
        start_bridge_fn=lambda: bridge,
        trigger_triage_fn=lambda p: triage_calls.append(p),
        sessions_root=sessions_root,
        indicator_fn=indicator,
    )
    assert rc == 0
    assert bridge.waited is True
    assert bridge.terminated is True
    assert triage_calls == [session_dir]
    assert started == ["dash", ("browser", "http://127.0.0.1:7860")]
    assert any("triage running" in m for m in msgs)


def test_does_not_restart_dashboard_when_already_up(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    started = []
    bridge = _FakeBridge()
    msgs, indicator = _collector()
    rc = run_recording_session(
        pod_reachable_fn=lambda: (True, "RUNNING"),
        dashboard_running_fn=lambda: True,
        start_dashboard_fn=lambda: started.append("dash"),
        open_browser_fn=lambda _u: None,
        start_bridge_fn=lambda: bridge,
        trigger_triage_fn=lambda _p: None,
        sessions_root=sessions_root,
        indicator_fn=indicator,
    )
    assert rc == 0
    assert "dash" not in started
    assert any("already running" in m for m in msgs)


# ---------------------------------------------------------------------------
# pod cold path
# ---------------------------------------------------------------------------


def test_pod_unreachable_clear_error_and_exits_two(tmp_path):
    msgs, indicator = _collector()
    started = []
    rc = run_recording_session(
        pod_reachable_fn=lambda: (False, "EXITED"),
        dashboard_running_fn=lambda: True,
        start_dashboard_fn=lambda: started.append("dash"),
        open_browser_fn=lambda _u: started.append("browser"),
        start_bridge_fn=lambda: started.append("bridge"),
        trigger_triage_fn=lambda _p: started.append("triage"),
        sessions_root=tmp_path / "sessions",
        indicator_fn=indicator,
    )
    assert rc == 2
    assert started == []  # nothing else ran
    assert any(POD_COLD_MESSAGE in m for m in msgs)


# ---------------------------------------------------------------------------
# Ctrl+C behaviour
# ---------------------------------------------------------------------------


def test_ctrl_c_during_wait_still_triages(tmp_path):
    sessions_root = tmp_path / "sessions"
    session_dir = _make_session(sessions_root, "s1")
    bridge = _FakeBridge()
    triage = []

    def _interrupt(_b):
        raise KeyboardInterrupt("simulated ctrl+c")

    msgs, indicator = _collector()
    rc = run_recording_session(
        pod_reachable_fn=lambda: (True, "RUNNING"),
        dashboard_running_fn=lambda: True,
        start_dashboard_fn=lambda: None,
        open_browser_fn=lambda _u: None,
        start_bridge_fn=lambda: bridge,
        trigger_triage_fn=lambda p: triage.append(p),
        sessions_root=sessions_root,
        indicator_fn=indicator,
        wait_fn=_interrupt,
    )
    assert rc == 0
    assert bridge.terminated is True
    assert triage == [session_dir]
    assert any("Ctrl+C" in m for m in msgs)


# ---------------------------------------------------------------------------
# no session dir -> triage skipped
# ---------------------------------------------------------------------------


def test_no_session_dir_triage_skipped(tmp_path):
    sessions_root = tmp_path / "empty"
    sessions_root.mkdir()
    bridge = _FakeBridge()
    triage = []
    msgs, indicator = _collector()
    rc = run_recording_session(
        pod_reachable_fn=lambda: (True, "RUNNING"),
        dashboard_running_fn=lambda: True,
        start_dashboard_fn=lambda: None,
        open_browser_fn=lambda _u: None,
        start_bridge_fn=lambda: bridge,
        trigger_triage_fn=lambda p: triage.append(p),
        sessions_root=sessions_root,
        indicator_fn=indicator,
    )
    assert rc == 0
    assert triage == []
    assert any("no session dir" in m.lower() for m in msgs)


# ---------------------------------------------------------------------------
# dashboard ping helper
# ---------------------------------------------------------------------------


def test_dashboard_running_fn_returns_false_on_connection_error(monkeypatch):
    def _boom(*_a, **_k):
        raise ConnectionError("no server")
    monkeypatch.setattr(
        record_runner.urllib.request, "urlopen", _boom,
    )
    assert record_runner.default_dashboard_running_fn("http://127.0.0.1:7860") is False


# ---------------------------------------------------------------------------
# latest session dir
# ---------------------------------------------------------------------------


def test_latest_session_dir_picks_newest(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _make_session(root, "2026-04-21T19-00-00")
    import time
    time.sleep(0.01)
    newest = _make_session(root, "2026-04-22T08-00-00")
    assert record_runner._latest_session_dir(root) == newest


def test_latest_session_dir_skips_staging(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _make_session(root, "real")
    staging = root / "_publish_staging" / "x"
    staging.mkdir(parents=True)
    (staging / "session_manifest.json").write_text("{}", encoding="utf-8")
    assert record_runner._latest_session_dir(root).name == "real"


def test_latest_session_dir_empty_returns_none(tmp_path):
    assert record_runner._latest_session_dir(tmp_path / "nope") is None


# ---------------------------------------------------------------------------
# scripts exist
# ---------------------------------------------------------------------------


def test_start_renee_recording_bat_exists():
    p = Path(__file__).resolve().parent.parent / "scripts" / "start_renee_recording.bat"
    assert p.exists()
    content = p.read_text(encoding="utf-8", errors="ignore")
    assert "record_runner" in content
    assert "RENEE_RECORD" in content


def test_start_renee_recording_ps1_exists():
    p = Path(__file__).resolve().parent.parent / "scripts" / "start_renee_recording.ps1"
    assert p.exists()
    content = p.read_text(encoding="utf-8", errors="ignore")
    assert "record_runner" in content
    assert "RENEE_RECORD" in content
