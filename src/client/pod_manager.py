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
from typing import Any, Optional

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
    # RunPod maps the container's audio_bridge_port to a different public TCP
    # port. None means "no NAT, dial the internal port directly" (local dev).
    audio_bridge_port_external: Optional[int] = None

    @property
    def bridge_url_template(self) -> str:
        port = self.audio_bridge_port_external or self.audio_bridge_port
        return f"ws://{{host}}:{port}"


def load_deployment(path: str | Path = DEFAULT_DEPLOY_CONFIG) -> DeploymentSettings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cloud = raw.get("cloud") or {}
    pod_id = os.environ.get("RENEE_POD_ID") or cloud.get("pod_id", "")
    external_raw = cloud.get("audio_bridge_port_external")
    return DeploymentSettings(
        mode=str(raw.get("mode", "cloud")),
        pod_id=str(pod_id or ""),
        region=str(cloud.get("region", "")),
        audio_bridge_port=int(cloud.get("audio_bridge_port", 8765)),
        eval_dashboard_port=int(cloud.get("eval_dashboard_port", 7860)),
        idle_shutdown_minutes=int(cloud.get("idle_shutdown_minutes", 60)),
        audio_bridge_port_external=int(external_raw) if external_raw is not None else None,
    )


def _lazy_runpod():
    import runpod  # type: ignore
    return runpod


# RunPod GPU tier mapping for the launcher's --gpu flag (#8). The values
# are RunPod's GPU type IDs as accepted by `create_pod`. "default" mirrors
# configs/deployment.yaml's `cloud.gpu_type` for steady-state work; "cheap"
# trades VRAM for $0.79/hr; "best" picks an H100 for cold-start speed.
GPU_TIERS: dict[str, str] = {
    "cheap": "NVIDIA L40S",
    "default": "NVIDIA A100 80GB PCIe",
    "best": "NVIDIA H100 80GB HBM3",
}


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
        """Start the pod; wait until it's actually up or timeout. Returns a summary dict."""
        if not self.settings.pod_id:
            raise RuntimeError("No pod_id configured (set RENEE_POD_ID or configs/deployment.yaml).")
        rp = self._client()
        rp.resume_pod(self.settings.pod_id, gpu_count=1)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            pod = rp.get_pod(self.settings.pod_id) or {}
            # desiredStatus flips to RUNNING as soon as resume is accepted;
            # uptimeSeconds > 0 is the signal that the container has actually
            # booted and we can hand out a bridge URL.
            if pod.get("desiredStatus") == "RUNNING" and (pod.get("uptimeSeconds") or 0) > 0:
                public_ip = _public_ip_from_pod(pod)
                return {
                    "status": "RUNNING",
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
        pod = rp.get_pod(self.settings.pod_id) or {}
        return {
            "id": self.settings.pod_id,
            "status": pod.get("desiredStatus", "UNKNOWN"),
            "public_ip": _public_ip_from_pod(pod),
            "uptime_seconds": pod.get("uptimeSeconds", 0) or 0,
            "gpu_type": (pod.get("machine") or {}).get("gpuDisplayName", ""),
        }

    # -------------------- provision (#1) --------------------

    def provision(
        self,
        *,
        gpu_type: str = "NVIDIA A100 80GB PCIe",
        name: str = "renee-auto",
        image: str = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        container_disk_gb: int = 80,
        volume_id: Optional[str] = None,
        volume_mount: str = "/workspace",
        ports_tcp: tuple[int, ...] = (8765, 22),
        update_yaml: bool = True,
        deploy_config_path: str | Path = DEFAULT_DEPLOY_CONFIG,
    ) -> dict:
        """Create a fresh pod with the audio bridge port + SSH exposed.

        This is the auto-provision path the launcher takes when status()
        reports NOT_CONFIGURED or the pod is gone. Updates
        configs/deployment.yaml with the new pod_id so subsequent wakes
        find it.

        Returns ``{"pod_id", "public_ip", "ports"}``. Raises on any
        unrecoverable error so the launcher can surface a clear hint.
        """
        rp = self._client()
        # The runpod SDK's create_pod signature changes between versions; we
        # call it with the most-stable kwargs and let the SDK ignore
        # unfamiliar ones. Volume attach is best-effort: not all runpod SDK
        # versions accept network_volume_id at create time, in which case
        # the operator attaches via the UI.
        kwargs: dict[str, Any] = {
            "name": name,
            "image_name": image,
            "gpu_type_id": gpu_type,
            "gpu_count": 1,
            "container_disk_in_gb": container_disk_gb,
            "ports": ",".join(f"{p}/tcp" for p in ports_tcp),
        }
        if volume_id:
            kwargs["network_volume_id"] = volume_id
            kwargs["volume_mount_path"] = volume_mount
        try:
            pod = rp.create_pod(**kwargs)
        except TypeError:
            # Older SDKs don't accept all kwargs; retry with the bare set.
            pod = rp.create_pod(
                name=name, image_name=image, gpu_type_id=gpu_type, gpu_count=1,
                container_disk_in_gb=container_disk_gb,
            )
        new_id = pod.get("id") if isinstance(pod, dict) else getattr(pod, "id", "")
        if not new_id:
            raise RuntimeError(f"create_pod returned no id: {pod!r}")

        # Update settings + persist new pod_id so subsequent calls find it
        self.settings = DeploymentSettings(
            mode=self.settings.mode, pod_id=str(new_id),
            region=self.settings.region,
            audio_bridge_port=self.settings.audio_bridge_port,
            eval_dashboard_port=self.settings.eval_dashboard_port,
            idle_shutdown_minutes=self.settings.idle_shutdown_minutes,
            audio_bridge_port_external=self.settings.audio_bridge_port_external,
        )
        if update_yaml:
            try:
                _persist_pod_id(deploy_config_path, str(new_id))
            except Exception:
                # Persistence is best-effort. The launcher prints the new
                # ID anyway so Paul can update by hand if this fails.
                pass

        # Probe for IP availability — the pod usually needs a few seconds
        # before its public-port mapping is announced.
        public_ip = ""
        for _ in range(40):
            time.sleep(2)
            info = rp.get_pod(new_id) or {}
            ip = _public_ip_from_pod(info)
            if ip:
                public_ip = ip
                break

        return {"pod_id": str(new_id), "public_ip": public_ip}


def _persist_pod_id(deploy_config_path: str | Path, new_id: str) -> None:
    """Rewrite ``cloud.pod_id`` in deployment.yaml in place. Preserves the
    rest of the file by surgically editing the matching line, so we don't
    drop comments / formatting that yaml.safe_dump would normalize away."""
    path = Path(deploy_config_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    rewrote = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("pod_id:"):
            indent = line[: len(line) - len(stripped)]
            lines[i] = f"{indent}pod_id: {new_id}\n"
            rewrote = True
            break
    if not rewrote:
        return
    path.write_text("".join(lines), encoding="utf-8")


def _public_ip_from_pod(pod: dict) -> str:
    runtime = pod.get("runtime") or {}
    for port in runtime.get("ports") or []:
        if port.get("isIpPublic") and port.get("ip"):
            return port["ip"]
    return ""
