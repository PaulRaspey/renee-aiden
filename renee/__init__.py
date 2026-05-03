"""`renee` launcher package. Aliases `src` so `python -m renee ...` works.

Also re-exports the ``renee.api`` programmatic surface so ``from renee
import wake_pod, pod_status, triage_session`` works without importing
``src`` directly. The CLI entry stays at ``renee.main``.
"""
from src.cli.main import main as main  # re-export

from .api import (  # noqa: F401  re-export
    PodInfo,
    TriageResult,
    WakeResult,
    cost_summary,
    latest_session_dir,
    pod_status,
    provision_pod,
    publish_list,
    publish_session,
    sleep_pod,
    triage_session,
    wake_pod,
)


__all__ = [
    "main",
    "PodInfo", "TriageResult", "WakeResult",
    "pod_status", "wake_pod", "sleep_pod", "provision_pod",
    "triage_session", "latest_session_dir",
    "publish_session", "publish_list",
    "cost_summary",
]
