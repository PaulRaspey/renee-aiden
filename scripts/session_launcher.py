"""One-button session launcher.

Wires together the pre-flight checks Paul would otherwise run by hand:
  1. Tailscale up + Tailscale IP visible (auto-up via auth-key when set)
  2. RunPod pod status (alive + has public IP); auto-provision when missing
  3. .env loaded (so RUNPOD_API_KEY etc. reach the SDK)
  4. (Optional) Beacon /v1/health reachable when BEACON_URL is set
  5. Wake the pod with retry on STARTING transition
  6. (Optional) Spawn local Beacon + Memory Bridge co-processes
  7. Start the dashboard in background (skipped if already running)
  8. Start the mobile proxy with HTTPS, cert, QR
  9. On Ctrl+C: shutdown clean + auto-trigger triage on the latest session

Each step is gated; failure halts with a remediation hint instead of
silently dropping into a broken session.

Use directly: ``python -m scripts.session_launcher``
Or via the .bat wrapper: ``scripts\\start_session.bat``
"""
from __future__ import annotations

import argparse
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


# ---------------------------------------------------------------------------
# Pre-flight: Tailscale (with optional auto-up via auth-key)
# ---------------------------------------------------------------------------


def _check_tailscale() -> tuple[bool, str]:
    """Returns (ok, info_or_error). If TAILSCALE_AUTHKEY is set and the IP
    isn't visible, attempt a headless `tailscale up --authkey=...` once
    before failing."""
    if shutil.which("tailscale") is None:
        return False, "tailscale CLI not on PATH"
    ip, err = _tailscale_ip()
    if ip:
        return True, ip
    # First probe failed. Try auto-up only if auth-key is set.
    authkey = os.environ.get("TAILSCALE_AUTHKEY", "").strip()
    if not authkey:
        return False, err or "no IPv4 — `tailscale up` needed?"
    print("      no Tailscale IP yet; running headless `tailscale up --authkey=...`", flush=True)
    try:
        proc = subprocess.run(
            ["tailscale", "up", f"--authkey={authkey}"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        return False, f"tailscale up failed: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip() or "tailscale up returned non-zero"
    # Re-probe after up
    ip, err = _tailscale_ip()
    if ip:
        return True, ip
    return False, err or "tailscale up succeeded but no IP yet"


def _tailscale_ip() -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        return "", f"tailscale ip failed: {e}"
    if proc.returncode != 0:
        return "", (proc.stderr or proc.stdout).strip() or "non-zero exit"
    ip = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    return ip, "" if ip else "no IPv4"


# ---------------------------------------------------------------------------
# Pre-flight: RunPod pod status (with auto-provision when missing/dead)
# ---------------------------------------------------------------------------


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


def _maybe_auto_provision(args: argparse.Namespace) -> tuple[bool, str]:
    """If --auto-provision is on AND the pod is missing/dead, ask before
    creating a fresh one. Returns (handled, message). When handled is True,
    caller should re-check pod status; when False, caller should fail with
    the message as the hint."""
    if not args.auto_provision:
        return False, ""
    try:
        from src.client.pod_manager import PodManager, load_deployment, GPU_TIERS
    except ImportError as e:
        return False, f"auto-provision needs PodManager.provision: {e}"
    settings = load_deployment("configs/deployment.yaml")
    gpu_type = GPU_TIERS.get(args.gpu, GPU_TIERS["default"])
    print(
        f"      no working pod; auto-provision will create one with GPU tier "
        f"'{args.gpu}' ({gpu_type}). This is a billing event.",
        flush=True,
    )
    if not args.yes:
        try:
            ans = input("      Type 'yes' to provision, anything else to cancel: ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "yes":
            return False, "auto-provision cancelled by operator"
    try:
        new = PodManager(settings).provision(gpu_type=gpu_type)
    except Exception as e:
        return False, f"provision failed: {e}"
    print(f"      provisioned pod_id={new.get('pod_id')} ip={new.get('public_ip', '?')}", flush=True)
    return True, ""


# ---------------------------------------------------------------------------
# Pre-flight: Beacon (soft) + dashboard liveness
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Wake with STARTING-transition retry (#10)
# ---------------------------------------------------------------------------


def _wake_with_retry(max_wait_s: int = 90) -> tuple[bool, dict]:
    """Wake the pod and poll until RUNNING+public_ip OR max_wait_s elapsed.

    PodManager.wake already polls internally for `desiredStatus == RUNNING`
    + uptime > 0, so a single wake() call usually suffices. This wrapper
    adds a second pass for the specific case where the first call returns
    something like STARTING — wait an additional grace and re-status.
    """
    from src.client.pod_manager import PodManager, load_deployment
    settings = load_deployment("configs/deployment.yaml")
    mgr = PodManager(settings)
    deadline = time.time() + max_wait_s
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        try:
            mgr.wake(wait_s=min(60, int(deadline - time.time())))
        except Exception as e:
            last_err = e
            time.sleep(3)
        info = mgr.status()
        if info.get("status") == "RUNNING" and info.get("public_ip"):
            return True, info
        time.sleep(2)
    return False, {"error": str(last_err) if last_err else "wake timed out"}


# ---------------------------------------------------------------------------
# Co-process spawning: Beacon + Memory Bridge (#3)
# ---------------------------------------------------------------------------


def _spawn_beacon() -> Optional[subprocess.Popen]:
    """If the user has Beacon-Prompt cloned at the canonical Downloads path
    AND has run pnpm install, spawn `pnpm dev` as a background process and
    set BEACON_URL to the local port.

    Returns the Popen handle (caller terminates on shutdown) or None when
    not feasible (no install, no path)."""
    candidates = [
        Path("C:/Users/Epsar/Downloads/Beacon-Prompt/Beacon-Prompt"),
        Path.home() / "Downloads" / "Beacon-Prompt" / "Beacon-Prompt",
    ]
    root = next((p for p in candidates if (p / "package.json").exists()), None)
    if root is None:
        print("      Beacon repo not found at C:\\Users\\Epsar\\Downloads\\Beacon-Prompt; skipping", flush=True)
        return None
    if not (root / "node_modules").exists():
        print(f"      Beacon at {root} but node_modules missing — run `pnpm install` once", flush=True)
        return None
    pnpm = shutil.which("pnpm") or shutil.which("corepack")
    if pnpm is None:
        print("      neither pnpm nor corepack on PATH; skipping Beacon", flush=True)
        return None
    cmd = [pnpm, "dev"] if pnpm.endswith("pnpm") or pnpm.endswith("pnpm.cmd") else [pnpm, "pnpm", "dev"]
    print(f"      spawning local Beacon at {root}", flush=True)
    proc = subprocess.Popen(
        cmd, cwd=str(root),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Default to the api-server's typical local port. Replit projects bind
    # PORT from env, so we set it explicitly when spawning.
    os.environ.setdefault("BEACON_URL", "http://127.0.0.1:8080")
    return proc


def _spawn_memory_bridge() -> Optional[subprocess.Popen]:
    """Spawn the Memory Bridge FastAPI app on its conventional port (8082)."""
    candidates = [
        Path("C:/Users/Epsar/Downloads/Memory-Bridge/Memory-Bridge/artifacts/api-server"),
        Path.home() / "Downloads" / "Memory-Bridge" / "Memory-Bridge" / "artifacts" / "api-server",
    ]
    root = next((p for p in candidates if (p / "run.py").exists()), None)
    if root is None:
        print("      Memory Bridge not found at C:\\Users\\Epsar\\Downloads\\Memory-Bridge; skipping", flush=True)
        return None
    print(f"      spawning local Memory Bridge at {root}", flush=True)
    env = dict(os.environ)
    env.setdefault("BRIDGE_TOKEN", "renee-local-handoff-token")
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


# ---------------------------------------------------------------------------
# Triage on Ctrl+C (#2)
# ---------------------------------------------------------------------------


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


def _trigger_triage(session_dir: Path) -> Optional[subprocess.Popen]:
    print(f"[stop] triage running on {session_dir.name} (background) ...", flush=True)
    return subprocess.Popen(
        [sys.executable, "-m", "renee", "triage", str(session_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Topic prompt (#4)
# ---------------------------------------------------------------------------


def _print_topic_banner(topic: str) -> None:
    """Big visual reminder so Paul says the topic as the first sentence.

    The orchestrator's greeting prompt is fixed at boot and a per-session
    override would require either env-var-on-the-pod or a new bridge
    message type. For now we surface the topic to the operator instead.
    """
    line = "=" * 60
    print(f"\n{line}", flush=True)
    print(f"  TOPIC FOR THIS SESSION: {topic}", flush=True)
    print(f"  Say this in your first sentence so Renée picks it up.", flush=True)
    print(f"{line}\n", flush=True)


# ---------------------------------------------------------------------------
# Cost telemetry (#5)
# ---------------------------------------------------------------------------


# Hourly USD rates for the GPU tiers we care about. Update as RunPod prices
# change; surfaces on the launcher's Ctrl+C summary so Paul can compare
# session value to spend.
GPU_HOURLY_USD = {
    "A100_SXM": 1.50,     # configs/deployment.yaml default
    "A100_PCIE": 1.20,
    "H100_SXM": 3.50,
    "H100_PCIE": 2.95,
    "L40S": 0.79,
    "RTX_4090": 0.44,
    "RTX_3090": 0.29,
    "default": 1.50,
}


def _pod_cost_summary(wake_at: float, gpu_type: str) -> str:
    elapsed_s = max(0, time.time() - wake_at)
    rate = GPU_HOURLY_USD.get(gpu_type, GPU_HOURLY_USD["default"])
    cost = (elapsed_s / 3600.0) * rate
    minutes = elapsed_s / 60.0
    return (
        f"pod up for {minutes:.1f} min; "
        f"GPU {gpu_type or '?'} @ ${rate:.2f}/hr; "
        f"this session ≈ ${cost:.2f}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="session_launcher",
        description="One-button Renée session launcher",
    )
    parser.add_argument("--topic", default=None, help="Print a topic banner before the session starts")
    parser.add_argument(
        "--gpu", default="default", choices=("cheap", "default", "best"),
        help="GPU tier when auto-provisioning (#1). cheap=L40S, default=A100, best=H100",
    )
    parser.add_argument(
        "--auto-provision", action="store_true",
        help="If pod is missing/dead, create a fresh one (asks for confirm unless --yes)",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip provision confirm prompt")
    parser.add_argument("--with-beacon", action="store_true", help="Spawn local Beacon co-process")
    parser.add_argument(
        "--with-memory-bridge", action="store_true",
        help="Spawn local Memory Bridge co-process for handoff capture",
    )
    parser.add_argument(
        "--no-triage-on-stop", action="store_true",
        help="Skip auto-triage of the latest session dir on Ctrl+C",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    os.chdir(REPO_ROOT)
    os.environ.setdefault("RENEE_SKIP_ENCRYPT_WARN", "1")

    if args.topic:
        _print_topic_banner(args.topic)

    total = 7
    # ---------------------------------------------------------- 1. tailscale
    _step(1, total, "Tailscale check ...")
    ts_ok, ts_info = _check_tailscale()
    if not ts_ok:
        return _fail(
            f"Tailscale not ready: {ts_info}",
            "Run: tailscale up   (or set TAILSCALE_AUTHKEY for headless auth)",
        )
    print(f"      tailscale ip: {ts_info}", flush=True)

    # --------------------------------------------------------------- 2. pod
    _step(2, total, "RunPod status ...")
    pod_ok, pod_info = _check_pod()
    if not pod_ok:
        # Auto-provision path (#1)
        handled, prov_msg = _maybe_auto_provision(args)
        if not handled:
            return _fail(
                f"Pod not running: {pod_info}",
                prov_msg or (
                    "Run: python -m renee wake   (or pass --auto-provision to create one)"
                ),
            )
        # Re-check after provision
        pod_ok, pod_info = _check_pod()
        if not pod_ok:
            return _fail(
                f"Pod still not running after provision: {pod_info}",
                "Inspect via RunPod UI",
            )
    print(f"      pod_id={pod_info.get('id', '?')} ip={pod_info.get('public_ip', '?')}", flush=True)
    pod_gpu_type = pod_info.get("gpu_type", "")

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

    # ---------------------------------------------------------- 4. wake retry
    _step(4, total, "Pod wake (with STARTING retry) ...")
    waked, wake_info = _wake_with_retry(max_wait_s=90)
    if not waked:
        return _fail(
            f"Pod did not transition to RUNNING within 90s: {wake_info}",
            "Inspect via RunPod UI; common cause is GPU unavailable in region",
        )
    pod_gpu_type = pod_gpu_type or wake_info.get("gpu_type", "")
    wake_at = time.time()
    print(f"      ready (gpu={pod_gpu_type or '?'})", flush=True)

    # -------------------------------------------------------- 5. co-process
    coprocs: list[subprocess.Popen] = []
    if args.with_beacon or args.with_memory_bridge:
        _step(5, total, "Co-processes ...")
        if args.with_beacon:
            p = _spawn_beacon()
            if p is not None:
                coprocs.append(p)
        if args.with_memory_bridge:
            p = _spawn_memory_bridge()
            if p is not None:
                coprocs.append(p)
    else:
        _step(5, total, "Co-processes (none requested) ...")

    # ---------------------------------------------------------- 6. dashboard
    _step(6, total, "Dashboard ...")
    dashboard_proc: Optional[subprocess.Popen] = None
    if _dashboard_running():
        print(f"      already running at {DASHBOARD_URL}", flush=True)
    else:
        print(f"      starting on {DASHBOARD_URL} ...", flush=True)
        dashboard_proc = _spawn_dashboard()
        for _ in range(20):
            if _dashboard_running():
                break
            time.sleep(0.1)

    # -------------------------------------------------------------- 7. proxy
    _step(7, total, "Mobile proxy (HTTPS + QR) ...")
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
        # Cost summary (#5)
        try:
            print(f"[stop] {_pod_cost_summary(wake_at, pod_gpu_type)}", flush=True)
        except Exception:
            pass

        # Trigger triage on the most recent session dir (#2)
        if not args.no_triage_on_stop:
            try:
                from src.capture.session_recorder import default_sessions_root
                latest = _latest_session_dir(default_sessions_root())
                if latest is not None:
                    _trigger_triage(latest)
                else:
                    print("[stop] no session dir found; triage skipped", flush=True)
            except Exception as e:
                print(f"[stop] triage trigger failed: {e}", flush=True)

        # Kill co-processes
        for p in coprocs:
            if p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

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
