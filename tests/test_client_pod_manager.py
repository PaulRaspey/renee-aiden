"""Tests for src.client.pod_manager."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.client.pod_manager import (
    DeploymentSettings,
    PodManager,
    load_deployment,
)


ROOT = Path(__file__).resolve().parents[1]


def test_load_deployment_parses_shipped_config(monkeypatch):
    monkeypatch.delenv("RENEE_POD_ID", raising=False)
    settings = load_deployment(ROOT / "configs" / "deployment.yaml")
    assert settings.mode == "cloud"
    assert settings.audio_bridge_port == 8765
    assert settings.eval_dashboard_port == 7860
    assert settings.idle_shutdown_minutes == 60
    assert settings.region  # non-empty from shipped config


def test_load_deployment_prefers_env_pod_id(monkeypatch):
    monkeypatch.setenv("RENEE_POD_ID", "env-pod-123")
    settings = load_deployment(ROOT / "configs" / "deployment.yaml")
    assert settings.pod_id == "env-pod-123"


def test_bridge_url_template_uses_configured_port():
    settings = DeploymentSettings(
        mode="cloud", pod_id="abc",
        region="US-TX",
        audio_bridge_port=9000,
        eval_dashboard_port=7860,
        idle_shutdown_minutes=60,
    )
    assert settings.bridge_url_template.format(host="1.2.3.4") == "ws://1.2.3.4:9000"


class FakeRunpod:
    def __init__(self):
        self.pod = SimpleNamespace(
            status="RUNNING",
            public_ip="1.2.3.4",
            uptime="5m",
            gpu_type="H100_SXM",
        )
        self.resumed = False
        self.stopped = False
        self.api_key = None

    def resume_pod(self, pod_id, gpu_count=1):
        self.resumed = True
        self.resumed_gpu_count = gpu_count
        self.pod.status = "RUNNING"

    def stop_pod(self, pod_id):
        self.stopped = True
        self.pod.status = "STOPPED"

    def get_pod(self, pod_id):
        return self.pod


def _settings() -> DeploymentSettings:
    return DeploymentSettings(
        mode="cloud",
        pod_id="pod-42",
        region="US-TX",
        audio_bridge_port=8765,
        eval_dashboard_port=7860,
        idle_shutdown_minutes=60,
    )


def test_wake_raises_without_pod_id():
    settings = DeploymentSettings(
        mode="cloud", pod_id="", region="", audio_bridge_port=8765,
        eval_dashboard_port=7860, idle_shutdown_minutes=60,
    )
    mgr = PodManager(settings, api_key="x")
    with pytest.raises(RuntimeError):
        mgr.wake()


def test_wake_returns_bridge_url_when_running(monkeypatch):
    mgr = PodManager(_settings(), api_key="x")
    fake = FakeRunpod()
    monkeypatch.setattr(mgr, "_client", lambda: fake)
    info = mgr.wake(wait_s=5, poll_interval_s=0)
    assert info["status"] == "RUNNING"
    assert info["public_ip"] == "1.2.3.4"
    assert info["bridge_url"] == "ws://1.2.3.4:8765"
    assert fake.resumed


def test_sleep_stops_pod(monkeypatch):
    mgr = PodManager(_settings(), api_key="x")
    fake = FakeRunpod()
    monkeypatch.setattr(mgr, "_client", lambda: fake)
    info = mgr.sleep()
    assert info["status"] == "STOPPED"
    assert fake.stopped


def test_status_for_unconfigured_pod():
    settings = DeploymentSettings(
        mode="cloud", pod_id="", region="", audio_bridge_port=8765,
        eval_dashboard_port=7860, idle_shutdown_minutes=60,
    )
    mgr = PodManager(settings)
    assert mgr.status() == {"status": "NOT_CONFIGURED"}


def test_status_reports_running_pod(monkeypatch):
    mgr = PodManager(_settings(), api_key="x")
    fake = FakeRunpod()
    monkeypatch.setattr(mgr, "_client", lambda: fake)
    info = mgr.status()
    assert info["status"] == "RUNNING"
    assert info["gpu_type"] == "H100_SXM"
