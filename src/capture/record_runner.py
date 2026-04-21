"""One-command recording session orchestration.

Used by scripts/start_renee_recording.bat (and .ps1) to stitch together
pod reachability check, dashboard warm-start, browser open, audio bridge
launch, and triage trigger on Ctrl+C. Every side effect is injectable so
the flow is unit-tested without a live pod, running dashboard, actual
browser, or real WAV files.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional


DEFAULT_DASHBOARD_URL = "http://127.0.0.1:7860"
DASHBOARD_PING_TIMEOUT_S = 2.0
POD_COLD_MESSAGE = "pod not reachable; run `python -m renee wake` first"


PodReachableFn = Callable[[], "tuple[bool, str]"]
DashboardRunningFn = Callable[[], bool]
StartDashboardFn = Callable[[], Any]
OpenBrowserFn = Callable[[str], None]
StartBridgeFn = Callable[[], Any]
TriggerTriageFn = Callable[[Path], Any]
WaitFn = Callable[[Any], None]


def default_pod_reachable_fn() -> "tuple[bool, str]":
    try:
        from src.client.pod_manager import PodManager, load_deployment
        settings = load_deployment("configs/deployment.yaml")
        info = PodManager(settings).status()
        running = info.get("status") == "RUNNING" and bool(info.get("public_ip"))
        return running, info.get("status") or "unknown"
    except Exception as e:
        return False, f"pod status check failed: {e}"


def default_dashboard_running_fn(url: str = DEFAULT_DASHBOARD_URL) -> bool:
    try:
        with urllib.request.urlopen(
            f"{url}/api/ping", timeout=DASHBOARD_PING_TIMEOUT_S,
        ) as r:
            return r.status == 200
    except Exception:
        return False


def default_start_dashboard_fn(_url: str = DEFAULT_DASHBOARD_URL) -> Any:
    return subprocess.Popen(
        [sys.executable, "-m", "src.dashboard"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def default_open_browser_fn(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def default_start_bridge_fn() -> Any:
    """Kick off the client-side audio bridge with RENEE_RECORD=1 so the
    pod-side orchestrator (once wired) will record the session. The .bat
    wrapper also sets the env var; we set it here too so CLI users of
    `python -m src.capture.record_runner` get the same behaviour."""
    env = dict(os.environ)
    env["RENEE_RECORD"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "renee", "talk"],
        env=env,
    )


def default_trigger_triage_fn(session_dir: Path) -> Any:
    return subprocess.Popen(
        [sys.executable, "-m", "renee", "triage", str(session_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def default_wait_fn(bridge: Any) -> None:
    if hasattr(bridge, "wait"):
        try:
            bridge.wait()
        except KeyboardInterrupt:
            raise


def _latest_session_dir(sessions_root: Path) -> Optional[Path]:
    if not sessions_root.exists():
        return None
    candidates = [
        p for p in sessions_root.iterdir()
        if p.is_dir() and not p.name.startswith("_")
        and (p / "session_manifest.json").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def run_recording_session(
    *,
    pod_reachable_fn: PodReachableFn,
    dashboard_running_fn: DashboardRunningFn,
    start_dashboard_fn: StartDashboardFn,
    open_browser_fn: OpenBrowserFn,
    start_bridge_fn: StartBridgeFn,
    trigger_triage_fn: TriggerTriageFn,
    sessions_root: Path,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    indicator_fn: Callable[[str], None] = None,
    wait_fn: Optional[WaitFn] = None,
    latest_session_dir_fn: Optional[Callable[[Path], Optional[Path]]] = None,
) -> int:
    """Run the full recording flow. Returns 0 on normal shutdown, 2 on
    pod-cold. Never raises; caller bubbles exit code."""
    indicator = indicator_fn or (lambda m: print(m, flush=True))
    reachable, note = pod_reachable_fn()
    if not reachable:
        indicator(f"{POD_COLD_MESSAGE} (status: {note})")
        return 2

    if dashboard_running_fn():
        indicator(f"[record] dashboard already running at {dashboard_url}")
    else:
        indicator(f"[record] starting dashboard on {dashboard_url}")
        start_dashboard_fn()
    open_browser_fn(dashboard_url)

    indicator(f"[record] session root: {sessions_root}")
    bridge = start_bridge_fn()

    wait = wait_fn or default_wait_fn
    try:
        wait(bridge)
    except KeyboardInterrupt:
        indicator("[record] Ctrl+C received; stopping bridge cleanly")

    if hasattr(bridge, "terminate"):
        try:
            bridge.terminate()
        except Exception:
            pass

    finder = latest_session_dir_fn or _latest_session_dir
    session_dir = finder(sessions_root)
    if session_dir is not None:
        trigger_triage_fn(session_dir)
        indicator("[record] triage running, check dashboard in ~5 min")
    else:
        indicator("[record] no session dir found; triage skipped")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="record_runner",
        description="One-command recording session orchestration",
    )
    parser.add_argument("--sessions-root", default=None)
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    args = parser.parse_args(argv)
    from src.capture.session_recorder import default_sessions_root
    sessions_root = (
        Path(args.sessions_root) if args.sessions_root else default_sessions_root()
    )
    return run_recording_session(
        pod_reachable_fn=default_pod_reachable_fn,
        dashboard_running_fn=lambda: default_dashboard_running_fn(args.dashboard_url),
        start_dashboard_fn=lambda: default_start_dashboard_fn(args.dashboard_url),
        open_browser_fn=default_open_browser_fn,
        start_bridge_fn=default_start_bridge_fn,
        trigger_triage_fn=default_trigger_triage_fn,
        sessions_root=sessions_root,
        dashboard_url=args.dashboard_url,
    )


if __name__ == "__main__":
    sys.exit(main())
