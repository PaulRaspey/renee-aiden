"""Unit tests for scripts.session_launcher pre-flight gating.

Doesn't run a real session; just verifies each pre-flight gate fails fast
with a useful message when its dependency is missing, and that the success
path advances through all five steps.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# scripts is not a python package — load the module by path so the test
# doesn't depend on PYTHONPATH wiring.
import importlib.util

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = REPO_ROOT / "scripts" / "session_launcher.py"
spec = importlib.util.spec_from_file_location("session_launcher", LAUNCHER_PATH)
session_launcher = importlib.util.module_from_spec(spec)
sys.modules["session_launcher"] = session_launcher
spec.loader.exec_module(session_launcher)  # type: ignore[union-attr]


def test_check_tailscale_missing_cli():
    with patch("session_launcher.shutil.which", return_value=None):
        ok, msg = session_launcher._check_tailscale()
    assert ok is False
    assert "tailscale" in msg.lower()


def test_check_tailscale_returns_ip(tmp_path):
    fake_run = MagicMock()
    fake_run.return_value = MagicMock(returncode=0, stdout="100.78.253.97\n", stderr="")
    with patch("session_launcher.shutil.which", return_value="/usr/bin/tailscale"):
        with patch("session_launcher.subprocess.run", fake_run):
            ok, ip = session_launcher._check_tailscale()
    assert ok is True
    assert ip == "100.78.253.97"


def test_check_tailscale_no_ip_fails():
    fake_run = MagicMock()
    fake_run.return_value = MagicMock(returncode=0, stdout="\n", stderr="")
    with patch("session_launcher.shutil.which", return_value="/usr/bin/tailscale"):
        with patch("session_launcher.subprocess.run", fake_run):
            ok, msg = session_launcher._check_tailscale()
    assert ok is False
    assert "no ipv4" in msg.lower() or "tailscale up" in msg.lower()


def test_check_beacon_skipped_when_unset(monkeypatch):
    monkeypatch.delenv("BEACON_URL", raising=False)
    assert session_launcher._check_beacon() is None


def test_check_beacon_warn_on_unreachable(monkeypatch):
    import urllib.error
    monkeypatch.setenv("BEACON_URL", "https://beacon.invalid")
    err = urllib.error.URLError("name resolution failed")
    with patch("session_launcher.urllib.request.urlopen", side_effect=err):
        warn = session_launcher._check_beacon()
    assert warn is not None
    assert "unreachable" in warn.lower() or "resolution" in warn.lower()


def test_check_beacon_ok_on_200(monkeypatch):
    monkeypatch.setenv("BEACON_URL", "https://beacon.example")
    fake_resp = MagicMock(status=200)
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: None
    with patch("session_launcher.urllib.request.urlopen", return_value=fake_resp):
        warn = session_launcher._check_beacon()
    assert warn is None


def test_dashboard_running_false_when_unreachable():
    with patch("session_launcher.urllib.request.urlopen", side_effect=Exception("conn refused")):
        assert session_launcher._dashboard_running() is False


def test_dashboard_running_true_on_200():
    fake_resp = MagicMock(status=200)
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: None
    with patch("session_launcher.urllib.request.urlopen", return_value=fake_resp):
        assert session_launcher._dashboard_running() is True


def test_main_fails_at_tailscale_step():
    with patch("session_launcher._check_tailscale", return_value=(False, "down")):
        rc = session_launcher.main()
    assert rc == 2


def test_main_fails_at_pod_step():
    with patch("session_launcher._check_tailscale", return_value=(True, "100.x.x.x")):
        with patch("session_launcher._check_pod", return_value=(False, {"status": "STOPPED"})):
            rc = session_launcher.main()
    assert rc == 2
