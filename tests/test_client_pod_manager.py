"""Tests for src.client.pod_manager."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.client.pod_manager import (
    DeploymentSettings,
    GPU_TIERS,
    PodManager,
    _persist_pod_id,
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


def test_bridge_url_template_prefers_external_port():
    settings = DeploymentSettings(
        mode="cloud", pod_id="abc",
        region="US-TX",
        audio_bridge_port=8765,
        eval_dashboard_port=7860,
        idle_shutdown_minutes=60,
        audio_bridge_port_external=10287,
    )
    assert settings.bridge_url_template.format(host="1.2.3.4") == "ws://1.2.3.4:10287"


def test_load_deployment_reads_external_port(monkeypatch):
    monkeypatch.delenv("RENEE_POD_ID", raising=False)
    settings = load_deployment(ROOT / "configs" / "deployment.yaml")
    # Shipped config maps internal 8765 -> public 10287; the template must
    # dial the public one or the OptiPlex client gets ConnectionRefused.
    assert settings.audio_bridge_port_external == 10287
    assert settings.bridge_url_template.format(host="1.2.3.4") == "ws://1.2.3.4:10287"


class FakeRunpod:
    def __init__(self):
        # Matches the real runpod.get_pod() dict shape.
        self.pod = {
            "id": "pod-42",
            "desiredStatus": "RUNNING",
            "uptimeSeconds": 42,
            "machine": {"gpuDisplayName": "A100 SXM"},
            "runtime": {
                "ports": [
                    {"ip": "10.0.0.1", "isIpPublic": False, "privatePort": 22,
                     "publicPort": 19000, "type": "tcp"},
                    {"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 22,
                     "publicPort": 18469, "type": "tcp"},
                ],
            },
        }
        self.resumed = False
        self.stopped = False
        self.api_key = None

    def resume_pod(self, pod_id, gpu_count=1):
        self.resumed = True
        self.resumed_gpu_count = gpu_count
        self.pod["desiredStatus"] = "RUNNING"
        self.pod["uptimeSeconds"] = 42

    def stop_pod(self, pod_id):
        self.stopped = True
        self.pod["desiredStatus"] = "EXITED"
        self.pod["uptimeSeconds"] = 0

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
    assert info["gpu_type"] == "A100 SXM"
    assert info["public_ip"] == "1.2.3.4"
    assert info["uptime_seconds"] == 42


def test_status_ignores_private_ips_when_selecting_public(monkeypatch):
    mgr = PodManager(_settings(), api_key="x")
    fake = FakeRunpod()
    # Swap the public IP entry to private — should return empty string.
    for p in fake.pod["runtime"]["ports"]:
        p["isIpPublic"] = False
    monkeypatch.setattr(mgr, "_client", lambda: fake)
    info = mgr.status()
    assert info["public_ip"] == ""


# -----------------------------------------------------------------------------
# Provision (#1) + GPU tiers (#8) + pod_id YAML rewrite
# -----------------------------------------------------------------------------


def test_gpu_tiers_has_required_keys():
    assert "cheap" in GPU_TIERS
    assert "default" in GPU_TIERS
    assert "best" in GPU_TIERS
    # All values are non-empty RunPod gpu_type_id strings
    for k, v in GPU_TIERS.items():
        assert v and isinstance(v, str)


class FakeRunpodCreate:
    """Captures create_pod kwargs + returns a pre-canned new pod with public IP."""

    def __init__(self, *, accept_volume: bool = True):
        self.api_key = None
        self.accept_volume = accept_volume
        self.create_calls: list[dict] = []
        self.created_pod_id = "pod-newly-minted"
        self._got_ip_after_calls = 1
        self._calls = 0

    def create_pod(self, **kwargs):
        if not self.accept_volume and "network_volume_id" in kwargs:
            raise TypeError("network_volume_id not accepted in this SDK version")
        self.create_calls.append(kwargs)
        return {"id": self.created_pod_id}

    def get_pod(self, pod_id):
        # Simulate "IP not yet announced" then "IP arrives" on the 2nd poll.
        self._calls += 1
        if self._calls < self._got_ip_after_calls:
            return {"id": pod_id, "runtime": {"ports": []}}
        return {
            "id": pod_id,
            "runtime": {
                "ports": [
                    {"ip": "9.8.7.6", "isIpPublic": True, "privatePort": 8765,
                     "publicPort": 11111, "type": "tcp"},
                ],
            },
        }


def test_provision_creates_pod_and_returns_ip(monkeypatch, tmp_path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text(
        "mode: cloud\n"
        "cloud:\n"
        "  pod_id: pod-old\n"
        "  region: US-TX\n"
        "  audio_bridge_port: 8765\n"
        "  audio_bridge_port_external: 10287\n",
        encoding="utf-8",
    )
    mgr = PodManager(_settings(), api_key="x")
    fake = FakeRunpodCreate()
    monkeypatch.setattr(mgr, "_client", lambda: fake)
    # Make the IP-poll loop fast so the test doesn't hang on time.sleep
    monkeypatch.setattr("src.client.pod_manager.time.sleep", lambda *_: None)
    result = mgr.provision(
        gpu_type=GPU_TIERS["default"], volume_id="vol-abc",
        deploy_config_path=cfg,
    )
    assert result["pod_id"] == "pod-newly-minted"
    assert result["public_ip"] == "9.8.7.6"
    assert mgr.settings.pod_id == "pod-newly-minted"
    # Volume args were forwarded
    call = fake.create_calls[0]
    assert call["network_volume_id"] == "vol-abc"
    assert call["volume_mount_path"] == "/workspace"
    # YAML now points at the new pod
    assert "pod-newly-minted" in cfg.read_text(encoding="utf-8")


def test_provision_falls_back_to_minimal_kwargs_on_typeerror(monkeypatch, tmp_path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text("mode: cloud\ncloud:\n  pod_id: pod-old\n", encoding="utf-8")
    mgr = PodManager(_settings(), api_key="x")
    fake = FakeRunpodCreate(accept_volume=False)
    monkeypatch.setattr(mgr, "_client", lambda: fake)
    monkeypatch.setattr("src.client.pod_manager.time.sleep", lambda *_: None)
    result = mgr.provision(
        gpu_type=GPU_TIERS["cheap"], volume_id="vol-abc",
        deploy_config_path=cfg,
    )
    assert result["pod_id"] == "pod-newly-minted"
    # First create raised TypeError, second succeeded
    assert len(fake.create_calls) == 1
    assert "network_volume_id" not in fake.create_calls[0]


def test_persist_pod_id_preserves_other_lines(tmp_path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text(
        "# comment line\n"
        "mode: cloud\n"
        "cloud:\n"
        "  pod_id: pod-old\n"
        "  region: US-TX  # nice region\n"
        "  audio_bridge_port: 8765\n",
        encoding="utf-8",
    )
    _persist_pod_id(cfg, "pod-NEW")
    text = cfg.read_text(encoding="utf-8")
    assert "pod_id: pod-NEW\n" in text
    assert "# comment line" in text  # comment preserved
    assert "# nice region" in text   # inline comment preserved
    assert "pod-old" not in text


def test_persist_pod_id_noops_when_no_pod_id_line(tmp_path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text("mode: cloud\nfoo: bar\n", encoding="utf-8")
    _persist_pod_id(cfg, "pod-NEW")
    # No pod_id line means we don't crash — just don't write.
    assert "pod-NEW" not in cfg.read_text(encoding="utf-8")
