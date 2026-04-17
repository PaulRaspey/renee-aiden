"""
RunPod pod lifecycle manager (M14).

Used by `python -m renee {wake, sleep, status}` to control the GPU pod
from PJ's OptiPlex. Depends on the `runpod` Python SDK; imports are
lazy so `python -m renee text` works on a box without the SDK
installed.

Config lives in configs/deployment.yaml (cloud.* keys). The pod ID
lives in the `RENEE_POD_ID` environment variable or in
configs/deployment.yaml under `cloud.pod_id`.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


logger = logging.getLogger("renee.client.pod_manager")


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEPLOY_CONFIG = REPO_ROOT / "configs" / "deployment.yaml"


@dataclass
class DeploymentSettings:
    mode: str                     # "cloud" | "local"
    pod_id: str
    region: str
    audio_bridge_port: int
    eval_dashboard_port: int
    idle_shutdown_minutes: int

    @property
    def bridge_url_template(self) -> str:
        return f"ws://{{host}}:{self.audio_bridge_port}"


def load_deployment(path: str | Path = DEFAULT_DEPLOY_CONFIG) -> DeploymentSettings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cloud = raw.get("cloud") or {}
    pod_id = os.environ.get("RENEE_POD_ID") or cloud.get("pod_id", "")
    return DeploymentSettings(
        mode=str(raw.get("mode", "cloud")),
        pod_id=str(pod_id or ""),
        region=str(cloud.get("region", "")),
        audio_bridge_port=int(cloud.get("audio_bridge_port", 8765)),
        eval_dashboard_port=int(cloud.get("eval_dashboard_port", 7860)),
        idle_shutdown_minutes=int(cloud.get("idle_shutdown_minutes", 60)),
    )


def _lazy_runpod():
    import runpod  # type: ignore
    return runpod


class PodManager:
    """
    Thin wrapper around the runpod SDK. Kept deliberately small so the
    unit tests exercise config parsing and command dispatch without
    touching the network.
    """

    def __init__(self, settings: DeploymentSettings, api_key: Optional[str] = None):
        self.settings = settings
        self.api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        self._runpod = None

    def _client(self):
        if self._runpod is None:
            rp = _lazy_runpod()
            rp.api_key = self.api_key
            self._runpod = rp
        return self._runpod

    # -------------------- commands --------------------

    def wake(self, *, wait_s: int = 180, poll_interval_s: int = 5) -> dict:
        """Start the pod; wait until it's RUNNING or timeout. Returns a summary dict."""
        if not self.settings.pod_id:
            raise RuntimeError("No pod_id configured (set RENEE_POD_ID or configs/deployment.yaml).")
        rp = self._client()
        rp.resume_pod(self.settings.pod_id)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            pod = rp.get_pod(self.settings.pod_id)
            status = getattr(pod, "status", "UNKNOWN")
            if status == "RUNNING":
                public_ip = getattr(pod, "public_ip", "")
                return {
                    "status": status,
                    "public_ip": public_ip,
                    "bridge_url": self.settings.bridge_url_template.format(host=public_ip),
                }
            time.sleep(poll_interval_s)
        raise TimeoutError(f"pod {self.settings.pod_id} not RUNNING within {wait_s}s")

    def sleep(self) -> dict:
        if not self.settings.pod_id:
            raise RuntimeError("No pod_id configured.")
        rp = self._client()
        rp.stop_pod(self.settings.pod_id)
        return {"status": "STOPPED", "pod_id": self.settings.pod_id}

    def status(self) -> dict:
        if not self.settings.pod_id:
            return {"status": "NOT_CONFIGURED"}
        rp = self._client()
        pod = rp.get_pod(self.settings.pod_id)
        return {
            "status": getattr(pod, "status", "UNKNOWN"),
            "public_ip": getattr(pod, "public_ip", ""),
            "uptime": getattr(pod, "uptime", ""),
            "gpu_type": getattr(pod, "gpu_type", ""),
        }
