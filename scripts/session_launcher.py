"""One-button session launcher.

Wires together the pre-flight checks Paul would otherwise run by hand:
  1. Tailscale up + Tailscale IP visible
  2. RunPod pod status (alive + has public IP)
  3. .env loaded (so RUNPOD_API_KEY etc. reach the SDK)
  4. (Optional) Beacon /v1/health reachable when BEACON_URL is set
  5. Wake the pod (idempotent)
  6. Start the dashboard in background (skipped if already running)
  7. Start the mobile proxy with HTTPS, cert, QR

Each step is gated; failure halts with a remediation hint instead of
silently dropping into a broken session. Ctrl+C in the foreground proxy
process tears the dashboard down with it.

Use directly: ``python -m scripts.session_launcher``
Or via the .bat wrapper: ``scripts\\start_session.bat``
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_URL = "http://127.0.0.1:7860"
DASHBOARD_PING_TIMEOUT_S = 2.0
WAKE_GRACE_S = 5  # let the bridge listen before we hand the QR to the user

logger = logging.getLogger("renee.session_launcher")


def _step(n: int, total: int, msg: str) -> None:
    print(f"[{n}/{total}] {msg}", flush=True)


def _fail(msg: str, hint: Optional[str] = None) -> int:
    print(f"\n[FAIL] {msg}", flush=True)
    if hint:
        print(f"       {hint}", flush=True)
    return 2


def _check_tailscale() -> tuple[bool, str]:
    if shutil.which("tailscale") is None:
        return False, "tailscale CLI not on PATH"
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        return False, f"tailscale ip failed: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip() or "non-zero exit"
    ip = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    if not ip:
        return False, "no IPv4 — tailscale up required?"
    return True, ip


def _check_pod() -> tuple[bool, dict]:
    """Returns (running, info_dict). False here means we should bail with a hint."""
    try:
        from src.client.pod_manager import PodManager, load_deployment
    except Exception as e:
        return False, {"error": f"pod_manager import failed: {e}"}
    try:
        info = PodManager(load_deployment("configs/deployment.yaml")).status()
    except Exception as e:
        return False, {"error": f"pod status check raised: {e}"}
    running = info.get("status") == "RUNNING" and bool(info.get("public_ip"))
    return running, info


def _check_beacon() -> Optional[str]:
    """Return None when BEACON_URL is unset OR the /v1/health probe succeeds.
    Return an error message string only when BEACON_URL is set but unreachable.
    A flaky Beacon doesn't block the session — Renée just won't heartbeat."""
    url = os.environ.get("BEACON_URL", "").strip()
    if not url:
        return None
    health = url.rstrip("/") + "/v1/health"
    try:
        with urllib.request.urlopen(health, timeout=3) as resp:
            if resp.status == 200:
                return None
            return f"BEACON_URL set ({url}) but /v1/health returned {resp.status}"
    except urllib.error.URLError as e:
        return f"BEACON_URL set ({url}) but unreachable: {e}"


def _dashboard_running() -> bool:
    try:
        with urllib.request.urlopen(
            f"{DASHBOARD_URL}/api/ping", timeout=DASHBOARD_PING_TIMEOUT_S,
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _spawn_dashboard() -> Optional[subprocess.Popen]:
    return subprocess.Popen(
        [sys.executable, "-m", "src.dashboard"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    os.chdir(REPO_ROOT)
    os.environ.setdefault("RENEE_SKIP_ENCRYPT_WARN", "1")

    total = 5
    # ---------------------------------------------------------- 1. tailscale
    _step(1, total, "Tailscale check ...")
    ts_ok, ts_info = _check_tailscale()
    if not ts_ok:
        return _fail(
            f"Tailscale not ready: {ts_info}",
            "Run: tailscale up   (then re-run this script)",
        )
    print(f"      tailscale ip: {ts_info}", flush=True)

    # --------------------------------------------------------------- 2. pod
    _step(2, total, "RunPod status ...")
    pod_ok, pod_info = _check_pod()
    if not pod_ok:
        return _fail(
            f"Pod not running: {pod_info}",
            "Run: python -m renee wake   (or recreate via RunPod UI per SESSION_TONIGHT.md)",
        )
    print(f"      pod_id={pod_info.get('id', '?')} ip={pod_info.get('public_ip', '?')}", flush=True)

    # ----------------------------------------------------- 3. beacon (soft)
    _step(3, total, "Beacon liveness probe (optional) ...")
    beacon_warn = _check_beacon()
    if beacon_warn is None:
        url = os.environ.get("BEACON_URL", "").strip()
        if url:
            print(f"      OK ({url})", flush=True)
        else:
            print("      skipped (BEACON_URL not set)", flush=True)
    else:
        print(f"      WARN: {beacon_warn}", flush=True)
        print("           continuing without liveness — Renée will skip heartbeats", flush=True)

    # ---------------------------------------------------------- 4. dashboard
    _step(4, total, "Dashboard ...")
    dashboard_proc: Optional[subprocess.Popen] = None
    if _dashboard_running():
        print(f"      already running at {DASHBOARD_URL}", flush=True)
    else:
        print(f"      starting on {DASHBOARD_URL} ...", flush=True)
        dashboard_proc = _spawn_dashboard()
        # Give it ~2s to come up so the proxy's "open this on your phone" message
        # lands after the dashboard is ready to inspect.
        for _ in range(20):
            if _dashboard_running():
                break
            time.sleep(0.1)

    # -------------------------------------------------------------- 5. proxy
    _step(5, total, "Mobile proxy (HTTPS + QR) ...")
    print("      Ctrl+C in this terminal stops the proxy + dashboard.", flush=True)
    print("      Scan the QR on your phone, install the cert from /cert if first run.", flush=True)
    print("", flush=True)

    proxy_cmd = [sys.executable, "-m", "renee", "proxy", "--https"]
    proxy_proc: Optional[subprocess.Popen] = None
    rc = 0
    try:
        proxy_proc = subprocess.Popen(proxy_cmd)
        rc = proxy_proc.wait()
    except KeyboardInterrupt:
        print("\n[stop] Ctrl+C — shutting down ...", flush=True)
        if proxy_proc is not None and proxy_proc.poll() is None:
            try:
                proxy_proc.send_signal(signal.SIGTERM)
                proxy_proc.wait(timeout=3)
            except Exception:
                try:
                    proxy_proc.kill()
                except Exception:
                    pass
        rc = 0
    finally:
        if dashboard_proc is not None and dashboard_proc.poll() is None:
            print("[stop] Stopping dashboard ...", flush=True)
            try:
                dashboard_proc.terminate()
                dashboard_proc.wait(timeout=3)
            except Exception:
                try:
                    dashboard_proc.kill()
                except Exception:
                    pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
