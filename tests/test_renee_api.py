"""Tests for the public renee.api surface (#5)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import renee
from renee import api as rapi


def test_re_exports_match_api_module():
    """The renee package must re-export everything api.__all__ promises."""
    for name in rapi.__all__:
        assert hasattr(renee, name), f"renee package missing re-export {name}"


def test_pod_status_wraps_status_dict():
    """pod_status returns a typed PodInfo from PodManager.status()."""
    fake_status = {
        "id": "pod-x", "status": "RUNNING", "public_ip": "1.2.3.4",
        "uptime_seconds": 600, "gpu_type": "A100 SXM",
    }
    with patch("renee.api.load_deployment", create=True):
        with patch("src.client.pod_manager.PodManager") as MgrCls:
            MgrCls.return_value.status.return_value = fake_status
            info = rapi.pod_status()
    assert isinstance(info, rapi.PodInfo)
    assert info.pod_id == "pod-x"
    assert info.is_running is True


def test_pod_info_is_running_requires_ip():
    info = rapi.PodInfo(
        pod_id="x", status="RUNNING", public_ip="",
        uptime_seconds=10, gpu_type="A100",
    )
    assert info.is_running is False  # no IP yet


def test_provision_pod_resolves_gpu_tier():
    with patch("src.client.pod_manager.PodManager") as MgrCls:
        with patch("src.client.pod_manager.load_deployment"):
            MgrCls.return_value.provision.return_value = {"pod_id": "new"}
            rapi.provision_pod(gpu_tier="cheap")
    call_kwargs = MgrCls.return_value.provision.call_args.kwargs
    assert call_kwargs["gpu_type"] == "NVIDIA L40S"


def test_provision_pod_falls_back_to_default_when_unset():
    with patch("src.client.pod_manager.PodManager") as MgrCls:
        with patch("src.client.pod_manager.load_deployment"):
            MgrCls.return_value.provision.return_value = {"pod_id": "new"}
            rapi.provision_pod()
    call_kwargs = MgrCls.return_value.provision.call_args.kwargs
    # Default tier resolves to A100
    assert "A100" in call_kwargs["gpu_type"]


def test_latest_session_dir_returns_newest(tmp_path):
    """Must skip underscored dirs and dirs without manifest, pick newest mtime."""
    import os
    import time
    for name, age in [("oldest", -3000), ("middle", -2000), ("newest", -100)]:
        d = tmp_path / name
        d.mkdir()
        (d / "session_manifest.json").write_text("{}")
        ts = time.time() + age
        os.utime(d, (ts, ts))
    (tmp_path / "_publish_staging").mkdir()
    (tmp_path / "_publish_staging" / "session_manifest.json").write_text("{}")
    (tmp_path / "no_manifest").mkdir()
    latest = rapi.latest_session_dir(tmp_path)
    assert latest is not None
    assert latest.name == "newest"


def test_latest_session_dir_returns_none_when_root_empty(tmp_path):
    assert rapi.latest_session_dir(tmp_path) is None
    assert rapi.latest_session_dir(tmp_path / "missing") is None


def test_triage_session_summarizes_flags(tmp_path):
    sd = tmp_path / "session-x"
    sd.mkdir()
    fake_raw = {
        "flags": [
            {"category": "fatigue", "severity": "low"},
            {"category": "safety", "severity": "high"},
            {"category": "safety", "severity": "medium"},
            {"category": "prosody", "severity": "low"},
        ],
        "fatigue_score": 0.42,
    }
    with patch("src.capture.triage.run_triage", return_value=fake_raw):
        result = rapi.triage_session(sd)
    assert result.flag_count == 4
    assert result.safety_count == 2
    assert result.fatigue_score == pytest.approx(0.42)
    assert result.session_dir == sd


def test_cost_summary_uses_status_and_rates():
    fake_status = {
        "id": "x", "status": "RUNNING", "public_ip": "1.2.3.4",
        "uptime_seconds": 1800,  # 30 min
        "gpu_type": "NVIDIA L40S",
    }
    with patch("src.client.pod_manager.PodManager") as MgrCls:
        with patch("src.client.pod_manager.load_deployment"):
            MgrCls.return_value.status.return_value = fake_status
            cost = rapi.cost_summary()
    assert cost["status"] == "RUNNING"
    assert cost["uptime_minutes"] == 30.0
    # L40S = $0.79/hr * 0.5h = $0.395 -> rounded to $0.4
    assert cost["hourly_usd"] == 0.79
    assert abs(cost["session_usd"] - 0.395) < 0.01


def test_publish_session_proxies_to_capture_publish():
    with patch("src.capture.publish.publish_session", return_value={"ok": True}) as p:
        with patch("src.capture.session_recorder.default_sessions_root", return_value=Path("X")):
            rapi.publish_session("session-1", confirm=True)
    assert p.call_args.args[1] == "session-1"
    assert p.call_args.kwargs["confirm"] is True


def test_publish_list_proxies_to_capture():
    with patch("src.capture.publish.list_publishable", return_value=[{"id": "s1"}]):
        with patch("src.capture.session_recorder.default_sessions_root", return_value=Path("X")):
            rows = rapi.publish_list()
    assert rows == [{"id": "s1"}]
