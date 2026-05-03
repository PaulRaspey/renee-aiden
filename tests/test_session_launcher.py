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
        rc = session_launcher.main([])
    assert rc == 2


def test_main_fails_at_pod_step():
    with patch("session_launcher._check_tailscale", return_value=(True, "100.x.x.x")):
        with patch("session_launcher._check_pod", return_value=(False, {"status": "STOPPED"})):
            rc = session_launcher.main([])
    assert rc == 2


# -----------------------------------------------------------------------------
# Tailscale auto-up via auth-key (#7)
# -----------------------------------------------------------------------------


def test_tailscale_no_authkey_returns_first_failure(monkeypatch):
    monkeypatch.delenv("TAILSCALE_AUTHKEY", raising=False)
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="\n", stderr=""))
    with patch("session_launcher.shutil.which", return_value="/usr/bin/tailscale"):
        with patch("session_launcher.subprocess.run", fake_run):
            ok, msg = session_launcher._check_tailscale()
    assert ok is False
    # `tailscale up` should NOT have been called — only the `ip` probe
    cmds = [call.args[0] for call in fake_run.call_args_list]
    for c in cmds:
        assert "up" not in c or "--authkey" not in str(c)


def test_tailscale_authkey_runs_up_then_reprobes(monkeypatch):
    monkeypatch.setenv("TAILSCALE_AUTHKEY", "tskey-xyz")
    # First probe: empty. After up: returns IP.
    runs = [
        MagicMock(returncode=0, stdout="\n", stderr=""),       # ip probe 1: empty
        MagicMock(returncode=0, stdout="up ok\n", stderr=""),  # up call
        MagicMock(returncode=0, stdout="100.99.88.77\n", stderr=""),  # ip probe 2
    ]
    fake_run = MagicMock(side_effect=runs)
    with patch("session_launcher.shutil.which", return_value="/usr/bin/tailscale"):
        with patch("session_launcher.subprocess.run", fake_run):
            ok, info = session_launcher._check_tailscale()
    assert ok is True
    assert info == "100.99.88.77"
    # Confirm the middle call is `tailscale up --authkey=...`
    up_call = fake_run.call_args_list[1].args[0]
    assert "up" in up_call
    assert any("--authkey=tskey-xyz" in a for a in up_call)


# -----------------------------------------------------------------------------
# Topic banner (#4)
# -----------------------------------------------------------------------------


def test_print_topic_banner_includes_topic(capsys):
    session_launcher._print_topic_banner("memory consolidation Part 3")
    out = capsys.readouterr().out
    assert "memory consolidation Part 3" in out
    assert "TOPIC" in out


# -----------------------------------------------------------------------------
# Cost telemetry (#5)
# -----------------------------------------------------------------------------


def test_pod_cost_summary_known_gpu():
    import time
    wake_at = time.time() - 3600  # 1 hour ago
    summary = session_launcher._pod_cost_summary(wake_at, "A100_SXM")
    # 1 hour at $1.50 = ~$1.50 (allow rounding)
    assert "1.50" in summary or "$1.5" in summary
    assert "A100_SXM" in summary


def test_pod_cost_summary_unknown_gpu_falls_back_to_default():
    import time
    wake_at = time.time() - 1800  # 30 min ago
    summary = session_launcher._pod_cost_summary(wake_at, "MysteryGPU")
    # Falls back to default rate ($1.50/hr); 30 min = ~$0.75
    assert "$0.7" in summary
    assert "MysteryGPU" in summary


# -----------------------------------------------------------------------------
# Triage on Ctrl+C (#2)
# -----------------------------------------------------------------------------


def test_latest_session_dir_returns_newest(tmp_path):
    import time
    # Three session dirs, manifests present, varying mtimes
    for name, age_offset in [("oldest", -3000), ("middle", -2000), ("newest", -100)]:
        d = tmp_path / name
        d.mkdir()
        (d / "session_manifest.json").write_text("{}")
        atime_mtime = time.time() + age_offset
        import os
        os.utime(d, (atime_mtime, atime_mtime))
    # _publish_staging-style underscored dirs are skipped
    (tmp_path / "_publish_staging").mkdir()
    (tmp_path / "_publish_staging" / "session_manifest.json").write_text("{}")
    latest = session_launcher._latest_session_dir(tmp_path)
    assert latest is not None
    assert latest.name == "newest"


def test_latest_session_dir_returns_none_for_empty(tmp_path):
    assert session_launcher._latest_session_dir(tmp_path) is None
    assert session_launcher._latest_session_dir(tmp_path / "missing") is None


def test_latest_session_dir_skips_dirs_without_manifest(tmp_path):
    d = tmp_path / "no_manifest"
    d.mkdir()
    # No session_manifest.json — should be excluded
    assert session_launcher._latest_session_dir(tmp_path) is None


# -----------------------------------------------------------------------------
# Argparse coverage
# -----------------------------------------------------------------------------


def test_parse_args_defaults():
    args = session_launcher._parse_args([])
    assert args.topic is None
    assert args.gpu == "default"
    assert args.auto_provision is False
    assert args.with_beacon is False
    assert args.with_memory_bridge is False
    assert args.no_triage_on_stop is False


def test_parse_args_all_flags():
    args = session_launcher._parse_args([
        "--topic", "session 7",
        "--gpu", "best",
        "--auto-provision", "--yes",
        "--with-beacon", "--with-memory-bridge",
        "--no-triage-on-stop",
    ])
    assert args.topic == "session 7"
    assert args.gpu == "best"
    assert args.auto_provision is True
    assert args.yes is True
    assert args.with_beacon is True
    assert args.with_memory_bridge is True
    assert args.no_triage_on_stop is True


def test_parse_args_rejects_invalid_gpu():
    with pytest.raises(SystemExit):
        session_launcher._parse_args(["--gpu", "ludicrous"])


# -----------------------------------------------------------------------------
# Daily cap pre-flight (#43)
# -----------------------------------------------------------------------------


def test_check_daily_cap_returns_none_without_safety_yaml(tmp_path, monkeypatch):
    """When safety.yaml is missing, _check_daily_cap returns None."""
    monkeypatch.setattr(session_launcher, "REPO_ROOT", tmp_path)
    assert session_launcher._check_daily_cap() is None


def test_check_daily_cap_full_when_no_db(tmp_path, monkeypatch):
    """Cap configured but no DB yet => 0 used, full remaining."""
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "safety.yaml").write_text(
        "health_monitor:\n  daily_cap_minutes: 120\n", encoding="utf-8",
    )
    monkeypatch.setattr(session_launcher, "REPO_ROOT", tmp_path)
    cap = session_launcher._check_daily_cap()
    assert cap is not None
    assert cap["cap_minutes"] == 120.0
    assert cap["used_minutes"] == 0.0
    assert cap["remaining_minutes"] == 120.0


def test_check_daily_cap_reflects_used(tmp_path, monkeypatch):
    """When the health monitor reports used minutes, remaining drops."""
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "safety.yaml").write_text(
        "health_monitor:\n  daily_cap_minutes: 90\n", encoding="utf-8",
    )
    (tmp_path / "state").mkdir()
    db = tmp_path / "state" / "renee_health.db"
    db.write_text("")  # any existing file triggers the live-monitor branch
    monkeypatch.setattr(session_launcher, "REPO_ROOT", tmp_path)

    fake_monitor = MagicMock()
    fake_monitor.daily_minutes.return_value = 35.0
    with patch("src.safety.health_monitor.HealthMonitor", return_value=fake_monitor):
        with patch("src.safety.health_monitor.HealthMonitorConfig"):
            cap = session_launcher._check_daily_cap()
    assert cap["used_minutes"] == 35.0
    assert cap["cap_minutes"] == 90.0
    assert cap["remaining_minutes"] == 55.0


def test_check_daily_cap_returns_none_when_no_cap_configured(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "safety.yaml").write_text(
        "health_monitor:\n  enabled: true\n", encoding="utf-8",
    )
    monkeypatch.setattr(session_launcher, "REPO_ROOT", tmp_path)
    assert session_launcher._check_daily_cap() is None
