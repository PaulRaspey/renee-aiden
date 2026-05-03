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
        new = PodManager(settings).provision(
            gpu_type=gpu_type,
            auto_volume_setup=args.with_volume_setup,
        )
    except Exception as e:
        return False, f"provision failed: {e}"
    print(f"      provisioned pod_id={new.get('pod_id')} ip={new.get('public_ip', '?')}", flush=True)
    if args.with_volume_setup:
        vs = new.get("volume_setup", "skipped")
        print(f"      volume_setup: {vs}", flush=True)
    return True, ""


# ---------------------------------------------------------------------------
# Pre-flight: Beacon (soft) + dashboard liveness
# ---------------------------------------------------------------------------


def _check_daily_cap() -> Optional[dict]:
    """Read the safety/health-monitor SQLite for today's used minutes vs the
    configured daily cap. Returns a dict with the relevant numbers, or None
    when the health monitor or config aren't set up yet.

    The launcher uses this to surface "X minutes left today" so Paul knows
    before connecting whether the session will trip the cap mid-conversation.
    """
    try:
        import yaml
        cfg_raw = yaml.safe_load(
            (REPO_ROOT / "configs" / "safety.yaml").read_text(encoding="utf-8"),
        ) or {}
    except Exception:
        return None
    hm = (cfg_raw.get("health_monitor") or {})
    cap = hm.get("daily_cap_minutes")
    if cap is None:
        return None
    db_path = REPO_ROOT / "state" / "renee_health.db"
    if not db_path.exists():
        # No monitor data yet; assume full cap available.
        return {"used_minutes": 0.0, "cap_minutes": float(cap), "remaining_minutes": float(cap)}
    try:
        from src.safety.health_monitor import HealthMonitor, HealthMonitorConfig
        # We only need daily_minutes() — defaults are fine for the read path.
        monitor = HealthMonitor(db_path, cfg=HealthMonitorConfig(daily_cap_minutes=int(cap)))
        used = float(monitor.daily_minutes())
    except Exception:
        return None
    remaining = max(0.0, float(cap) - used)
    return {
        "used_minutes": round(used, 1),
        "cap_minutes": float(cap),
        "remaining_minutes": round(remaining, 1),
    }


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


def _prompt_presence_score(session_dir: Path) -> None:
    """Ask Paul to rate 1-5 and write into the session manifest. Empty
    input or non-numeric skips silently — no score is better than a
    coerced wrong score, and review tooling already handles missing
    presence_score gracefully."""
    print("", flush=True)
    print(f"How was that session? (1-5, blank to skip)", flush=True)
    try:
        raw = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not raw:
        return
    try:
        score = int(raw)
    except ValueError:
        print(f"      not a number: {raw!r}; skipping", flush=True)
        return
    if not (1 <= score <= 5):
        print(f"      out of range: {score}; skipping", flush=True)
        return
    try:
        from src.capture.dashboard_sessions import set_presence_score
        from src.capture.session_recorder import default_sessions_root
        set_presence_score(default_sessions_root(), session_dir.name, score)
        print(f"      recorded presence_score={score} on {session_dir.name}", flush=True)
    except Exception as e:
        print(f"      failed to persist score: {e}", flush=True)


# ---------------------------------------------------------------------------
# Topic prompt (#4)
# ---------------------------------------------------------------------------


def _print_topic_banner(topic: str) -> None:
    """Big visual reminder + URL hint.

    The PWA's connect URL accepts ``?topic=...``; client.js sends a
    ``set_topic`` WS message on connect, the audio bridge dispatches it
    to ``orchestrator.set_session_topic``, and ``greet_on_connect`` uses
    it in the first prompt. The launcher prints the topic so Paul also
    sees it before the proxy's QR shows up.
    """
    line = "=" * 60
    print(f"\n{line}", flush=True)
    print(f"  TOPIC FOR THIS SESSION: {topic}", flush=True)
    print(f"  Connect via QR or URL with ?topic=<urlencoded> so Renée's", flush=True)
    print(f"  first greeting acknowledges it. Tap-to-start kicks off the", flush=True)
    print(f"  set_topic message automatically when this query is present.", flush=True)
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
    parser.add_argument(
        "--with-volume-setup", action="store_true",
        help="After --auto-provision, SSH to the new pod and run volume_setup.py automatically",
    )
    parser.add_argument("--with-beacon", action="store_true", help="Spawn local Beacon co-process")
    parser.add_argument(
        "--with-memory-bridge", action="store_true",
        help="Spawn local Memory Bridge co-process for handoff capture",
    )
    parser.add_argument(
        "--no-triage-on-stop", action="store_true",
        help="Skip auto-triage of the latest session dir on Ctrl+C",
    )
    parser.add_argument(
        "--no-score-prompt", action="store_true",
        help="Skip the 1-5 presence-score prompt at the end of the session",
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip auto-generating report.md inside the latest session dir",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    os.chdir(REPO_ROOT)
    os.environ.setdefault("RENEE_SKIP_ENCRYPT_WARN", "1")

    # Pull keyring-stored secrets into env so existing .env-based code paths
    # (RUNPOD_API_KEY, GROQ_API_KEY, etc.) keep working transparently. Env
    # values already set win — keyring only fills gaps. Failure modes
    # (no keyring backend) are silently skipped per renee.secrets.
    try:
        from renee import secrets as _secrets
        _secrets.populate_env_from_keyring()
    except Exception:
        # Keyring is opt-in — never block startup on it.
        pass

    if args.topic:
        _print_topic_banner(args.topic)
        # Plumb to the proxy via env so its QR bakes in ?topic=...
        os.environ["RENEE_SESSION_TOPIC"] = args.topic

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

    # Daily cap budget — surfaced as a pre-flight info line so Paul sees how
    # much session time is left before he connects. Doesn't gate the session.
    cap = _check_daily_cap()
    if cap is not None:
        used, total_cap = cap["used_minutes"], cap["cap_minutes"]
        remaining = cap["remaining_minutes"]
        flag = "" if remaining > 30 else (" [low]" if remaining > 0 else " [CAP REACHED]")
        print(
            f"      daily cap: {used:.0f} of {total_cap:.0f} min used; "
            f"{remaining:.0f} min remaining today{flag}",
            flush=True,
        )

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

    # Cost ledger: record this pod-up so the dashboard's monthly view sees it.
    try:
        from src.client.cost_ledger import record_up as _ledger_up
        _ledger_up(
            pod_id=pod_info.get("id", "") or "",
            gpu_type=pod_gpu_type,
            hourly_usd=GPU_HOURLY_USD.get(pod_gpu_type, GPU_HOURLY_USD["default"]),
        )
    except Exception:
        pass

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

        # Cost ledger: record pod-down for the monthly view.
        elapsed_min = max(0.0, (time.time() - wake_at) / 60.0)
        try:
            from src.client.cost_ledger import record_down as _ledger_down
            _ledger_down(
                pod_id=pod_info.get("id", "") or "",
                minutes=elapsed_min,
                hourly_usd=GPU_HOURLY_USD.get(pod_gpu_type, GPU_HOURLY_USD["default"]),
                gpu_type=pod_gpu_type,
            )
        except Exception:
            pass

        # Memory Bridge auto-capture: publish a session-end handoff so the
        # next Claude session has context. Skipped silently when the env
        # vars aren't set.
        try:
            from src.client.memory_bridge_client import (
                MemoryBridgeClient, build_session_handoff,
            )
            mb = MemoryBridgeClient.from_env()
            if mb is not None:
                rate = GPU_HOURLY_USD.get(pod_gpu_type, GPU_HOURLY_USD["default"])
                cost_summary = {
                    "uptime_minutes": round(elapsed_min, 1),
                    "gpu_type": pod_gpu_type,
                    "session_usd": round(elapsed_min / 60.0 * rate, 2),
                }
                payload = build_session_handoff(
                    thread_name=os.environ.get("MEMORY_BRIDGE_THREAD", "renee-voice"),
                    topic=args.topic,
                    pod_id=pod_info.get("id"),
                    cost_summary=cost_summary,
                )
                resp = mb.publish(payload)
                if resp is not None:
                    print(
                        f"[stop] memory-bridge handoff captured "
                        f"({resp.get('handoff_id', '?')})", flush=True,
                    )
        except Exception:
            pass

        # Trigger triage on the most recent session dir (#2)
        latest_session: Optional[Path] = None
        if not args.no_triage_on_stop:
            try:
                from src.capture.session_recorder import default_sessions_root
                latest_session = _latest_session_dir(default_sessions_root())
                if latest_session is not None:
                    _trigger_triage(latest_session)
                else:
                    print("[stop] no session dir found; triage skipped", flush=True)
            except Exception as e:
                print(f"[stop] triage trigger failed: {e}", flush=True)

        # Eval score prompt — captures Paul's 1-5 score directly into the
        # session manifest while the conversation's still fresh. Skipped on
        # non-TTY stdin (CI / scripted runs) and when --no-score-prompt is set.
        if (latest_session is not None
                and not args.no_score_prompt
                and sys.stdin.isatty()):
            try:
                _prompt_presence_score(latest_session)
            except Exception as e:
                print(f"[stop] presence score prompt failed: {e}", flush=True)

        # Auto-generate the post-session report so tonight's documented
        # sessions land with a Markdown summary next to the artifacts.
        # Triage may still be running in background — the report will pick
        # up triage results on its next regeneration if Paul re-runs
        # `renee report <session>` once triage finishes.
        if latest_session is not None and not args.no_report:
            try:
                from src.capture.report import write_report
                report_path = write_report(latest_session)
                print(f"[stop] report -> {report_path}", flush=True)
            except Exception as e:
                print(f"[stop] report write failed: {e}", flush=True)

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
