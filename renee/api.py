"""Programmatic API for renee subcommands.

The CLI entry points in ``src.cli.main`` parse argparse Namespaces and
print JSON. Calling them from Python (e.g. ``scripts/session_launcher.py``)
forced shelling out to ``python -m renee ...``: more processes, lost
exit codes wrapped in subprocess.CalledProcessError, and no way to
bridge progress.

This module exposes thin wrappers that mirror the CLI surface but take
typed kwargs and return rich result objects. Keeping a separate module
(rather than re-exporting cmd_* directly) means we can change CLI
internals without breaking programmatic callers.

Stability: this is the public Python surface. New subcommands MUST be
added here too. Renames go through a one-release deprecation alias.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("renee.api")


@dataclass
class PodInfo:
    """Read model of pod_manager.status()."""
    pod_id: str
    status: str
    public_ip: str
    uptime_seconds: int
    gpu_type: str

    @classmethod
    def from_status(cls, info: dict) -> "PodInfo":
        return cls(
            pod_id=str(info.get("id") or ""),
            status=str(info.get("status") or "UNKNOWN"),
            public_ip=str(info.get("public_ip") or ""),
            uptime_seconds=int(info.get("uptime_seconds") or 0),
            gpu_type=str(info.get("gpu_type") or ""),
        )

    @property
    def is_running(self) -> bool:
        return self.status == "RUNNING" and bool(self.public_ip)


@dataclass
class WakeResult:
    pod_id: str
    status: str
    public_ip: str
    bridge_url: str


@dataclass
class TriageResult:
    session_dir: Path
    flag_count: int = 0
    safety_count: int = 0
    fatigue_score: float = 0.0
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# pod lifecycle
# ---------------------------------------------------------------------------


def pod_status(deploy_config: str | Path = "configs/deployment.yaml") -> PodInfo:
    """Read-only pod status. No billing event."""
    from src.client.pod_manager import PodManager, load_deployment
    settings = load_deployment(deploy_config)
    return PodInfo.from_status(PodManager(settings).status())


def wake_pod(
    *,
    deploy_config: str | Path = "configs/deployment.yaml",
    wait_s: int = 180,
) -> WakeResult:
    """Resume the pod. Idempotent on already-RUNNING pods. Billing event
    on STOPPED pods."""
    from src.client.pod_manager import PodManager, load_deployment
    settings = load_deployment(deploy_config)
    info = PodManager(settings).wake(wait_s=wait_s)
    return WakeResult(
        pod_id=settings.pod_id,
        status=str(info.get("status", "UNKNOWN")),
        public_ip=str(info.get("public_ip", "")),
        bridge_url=str(info.get("bridge_url", "")),
    )


def sleep_pod(*, deploy_config: str | Path = "configs/deployment.yaml") -> dict:
    """Stop the pod. Returns the sleep() result dict."""
    from src.client.pod_manager import PodManager, load_deployment
    return PodManager(load_deployment(deploy_config)).sleep()


def provision_pod(
    *,
    gpu_type: Optional[str] = None,
    gpu_tier: Optional[str] = None,
    auto_volume_setup: bool = False,
    deploy_config: str | Path = "configs/deployment.yaml",
) -> dict:
    """Create a new pod. Pass either ``gpu_type`` (raw RunPod ID) or
    ``gpu_tier`` ('cheap'|'default'|'best') — gpu_tier wins if both set.
    """
    from src.client.pod_manager import (
        GPU_TIERS, PodManager, load_deployment,
    )
    if gpu_tier:
        gpu_type = GPU_TIERS.get(gpu_tier, GPU_TIERS["default"])
    if not gpu_type:
        gpu_type = GPU_TIERS["default"]
    settings = load_deployment(deploy_config)
    return PodManager(settings).provision(
        gpu_type=gpu_type,
        auto_volume_setup=auto_volume_setup,
        deploy_config_path=deploy_config,
    )


# ---------------------------------------------------------------------------
# capture / triage / publish
# ---------------------------------------------------------------------------


def triage_session(session_dir: str | Path) -> TriageResult:
    """Run the triage pipeline on a session dir. Mirrors `renee triage`.
    Returns counts so callers can build a quick summary line."""
    from src.capture.triage import run_triage
    sd = Path(session_dir)
    raw = run_triage(sd)
    flags = raw.get("flags") or []
    safety = [f for f in flags if (f.get("category") or "").lower() == "safety"]
    fatigue_score = float(raw.get("fatigue_score") or 0.0)
    return TriageResult(
        session_dir=sd,
        flag_count=len(flags),
        safety_count=len(safety),
        fatigue_score=fatigue_score,
        raw=raw,
    )


def latest_session_dir(sessions_root: Optional[Path] = None) -> Optional[Path]:
    """Return the newest session dir under sessions_root, or None when empty."""
    from src.capture.session_recorder import default_sessions_root
    root = Path(sessions_root) if sessions_root else default_sessions_root()
    if not root.exists():
        return None
    candidates = [
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith("_")
        and (p / "session_manifest.json").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def publish_session(
    session_id: str,
    *,
    confirm: bool = False,
    sessions_root: Optional[Path] = None,
) -> dict:
    """Mirror of `renee publish`. Without confirm, only stages."""
    from src.capture.publish import publish_session as _publish
    from src.capture.session_recorder import default_sessions_root
    root = Path(sessions_root) if sessions_root else default_sessions_root()
    return _publish(root, session_id, confirm=confirm)


def publish_list(*, sessions_root: Optional[Path] = None) -> list[dict]:
    """List sessions eligible for publishing."""
    from src.capture.publish import list_publishable
    from src.capture.session_recorder import default_sessions_root
    root = Path(sessions_root) if sessions_root else default_sessions_root()
    return list_publishable(root)


# ---------------------------------------------------------------------------
# session helpers
# ---------------------------------------------------------------------------


def cost_summary(*, deploy_config: str | Path = "configs/deployment.yaml") -> dict:
    """Compute pod-up-cost from current status. Mirrors what the dashboard
    /api/cost endpoint returns. Useful for scripts that want a one-shot
    cost snapshot without running an HTTP server."""
    info = pod_status(deploy_config=deploy_config)
    rates = {
        "A100 SXM": 1.50, "A100 PCIe": 1.20, "A100 80GB PCIe": 1.20,
        "H100 SXM": 3.50, "H100 PCIe": 2.95, "H100 80GB HBM3": 3.50,
        "L40S": 0.79, "RTX 4090": 0.44, "RTX 3090": 0.29,
    }
    rate = next(
        (v for k, v in rates.items() if k.lower() in info.gpu_type.lower()),
        1.50,
    )
    cost = (info.uptime_seconds / 3600.0) * rate
    return {
        "status": info.status,
        "gpu_type": info.gpu_type,
        "uptime_minutes": round(info.uptime_seconds / 60.0, 1),
        "hourly_usd": rate,
        "session_usd": round(cost, 2),
    }


__all__ = [
    "PodInfo", "WakeResult", "TriageResult",
    "pod_status", "wake_pod", "sleep_pod", "provision_pod",
    "triage_session", "latest_session_dir",
    "publish_session", "publish_list",
    "cost_summary",
]
