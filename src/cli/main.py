"""
`python -m renee` CLI entry point (M14).

Subcommands:
  wake      start the RunPod GPU pod; wait until the bridge is ready
  talk      open the audio bridge from OptiPlex to the running pod
  sleep     graceful pod shutdown (saves state, stops billing)
  status    print pod status + bridge URL
  text      local text-mode REPL (no cloud GPU required)
  eval      run the eval harness
  export    export state to a directory
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[2]


try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass


def _emit_encryption_warning() -> None:
    if os.environ.get("RENEE_SKIP_ENCRYPT_WARN") == "1":
        return
    try:
        import yaml
        cfg = yaml.safe_load(
            (REPO_ROOT / "configs" / "safety.yaml").read_text(encoding="utf-8")
        ) or {}
        enabled = bool((cfg.get("memory_encryption") or {}).get("enabled", False))
    except Exception:
        return
    if not enabled:
        print(
            "warning: memory_encryption.enabled=false — plaintext vault. "
            "RENEE_SKIP_ENCRYPT_WARN=1 to silence.",
            file=sys.stderr,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="renee", description="Talk to Renée.")
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("wake", help="start the cloud pod")
    sub.add_parser("talk", help="open audio bridge to the running pod")
    sub.add_parser("sleep", help="graceful pod shutdown")
    sub.add_parser("status", help="show pod status")
    sub.add_parser("text", help="local text-mode REPL")
    sub.add_parser("eval", help="run the eval harness")
    sub.add_parser("check-deps", help="report which optional audio/cloud deps are missing")

    proxy_p = sub.add_parser(
        "proxy",
        help="serve the mobile PWA (phone browser -> proxy -> bridge)",
    )
    proxy_p.add_argument(
        "--port", type=int, default=None,
        help="proxy port (default: cloud.proxy_port in deployment.yaml or 8766)",
    )
    proxy_p.add_argument(
        "--bridge-url", default=None,
        help="override bridge URL (skips pod status lookup)",
    )
    proxy_p.add_argument(
        "--no-browser", action="store_true",
        help="(default) do not auto-open a browser; listed for forward compat",
    )
    proxy_p.add_argument(
        "--https", action="store_true",
        help="enable self-signed HTTPS (required by iOS Safari getUserMedia)",
    )

    export_p = sub.add_parser("export", help="export state to a directory")
    export_p.add_argument(
        "--output", default=str(REPO_ROOT / "exports"),
        help="destination directory",
    )
    export_p.add_argument(
        "--dry-run", action="store_true",
        help="list files that would be exported without copying",
    )

    triage_p = sub.add_parser("triage", help="run the post-session triage pipeline")
    triage_p.add_argument("session_dir", help="path to a session directory")
    triage_p.add_argument(
        "--whisper-model", default="base.en",
        help="WhisperX model size (default base.en)",
    )

    highlights_p = sub.add_parser(
        "highlights",
        help="regenerate HIGHLIGHTS.md from tagged notes across sessions",
    )
    highlights_p.add_argument(
        "--sessions-root", default=None,
        help="override RENEE_SESSIONS_DIR / default sessions root",
    )

    publish_p = sub.add_parser("publish", help="package and optionally push a session")
    publish_p.add_argument("session_id", help="session id to publish")
    publish_p.add_argument(
        "--include-audio", action="store_true",
        help="include 48kbps mono Opus derivatives (WAV masters never leave)",
    )
    publish_p.add_argument(
        "--confirm", action="store_true",
        help="actually commit and push; without this only staging is written",
    )

    sub.add_parser(
        "publish-list",
        help="list sessions marked public but not yet published",
    )

    dash_p = sub.add_parser(
        "dashboard",
        help="open the M15 dashboard in a browser (auto-starts if not running)",
    )
    dash_p.add_argument(
        "--port", type=int, default=7860,
        help="dashboard port (default 7860)",
    )
    dash_p.add_argument(
        "--no-browser", action="store_true",
        help="just start/verify the dashboard; don't open a browser",
    )

    logs_p = sub.add_parser(
        "logs",
        help="tail conversation logs from state/logs/conversations/",
    )
    logs_p.add_argument(
        "--day", default=None,
        help="YYYY-MM-DD; default is today (UTC)",
    )
    logs_p.add_argument(
        "-f", "--follow", action="store_true",
        help="follow the file as new turns are appended",
    )
    logs_p.add_argument(
        "-n", "--tail", type=int, default=50,
        help="show this many last lines before following (default 50)",
    )

    sub.add_parser(
        "migrate-secrets",
        help="copy known secrets from .env into the OS keyring (one-time)",
    )

    backup_p = sub.add_parser(
        "backup",
        help="run a one-shot backup per deployment.yaml backup config",
    )
    backup_p.add_argument(
        "--force", action="store_true",
        help="ignore backup.enabled in deployment.yaml and back up anyway",
    )
    backup_p.add_argument(
        "--check", action="store_true",
        help="list existing archives + manifest tail; do nothing else",
    )

    sub.add_parser(
        "preflight",
        help="run all the launcher's pre-flight checks (tailscale, pod, beacon, cap) and exit",
    )

    sub.add_parser(
        "version",
        help="print the renee build version + key dependency versions",
    )

    fetch_p = sub.add_parser(
        "fetch-logs",
        help="pull /workspace/state/logs/conversations/ from the pod via SFTP",
    )
    fetch_p.add_argument(
        "--dest", default=None,
        help="local directory (default: state/logs/conversations/)",
    )
    fetch_p.add_argument(
        "--ssh-key", default=None,
        help="SSH private key path (default RENEE_POD_SSH_KEY or ~/.ssh/id_rsa)",
    )

    sessions_p = sub.add_parser(
        "sessions",
        help="list captured sessions with id / time / duration / score",
    )
    sessions_p.add_argument(
        "--day", default=None,
        help="filter to YYYY-MM-DD (UTC); default: all days",
    )
    sessions_p.add_argument(
        "--sessions-root", default=None,
        help="override RENEE_SESSIONS_DIR / default sessions root",
    )

    report_p = sub.add_parser(
        "report",
        help="generate a Markdown report.md inside a session directory",
    )
    report_p.add_argument("session_id", help="session id under the sessions root")
    report_p.add_argument(
        "--sessions-root", default=None,
        help="override RENEE_SESSIONS_DIR / default sessions root",
    )
    report_p.add_argument(
        "--print", action="store_true", dest="print_only",
        help="print the report to stdout instead of writing report.md",
    )

    beacon_p = sub.add_parser(
        "beacon-setup",
        help="fetch Beacon's public key + (optionally) register a webhook",
    )
    beacon_p.add_argument(
        "--url", required=True,
        help="Base URL of the Beacon deploy (e.g. https://beacon.example)",
    )
    beacon_p.add_argument(
        "--agent-id", default=None,
        help="Agent ID to PATCH webhook_url onto; if omitted, only fetches the key",
    )
    beacon_p.add_argument(
        "--api-key", default=None,
        help="Agent's api_key for the PATCH (defaults to BEACON_API_KEY env)",
    )
    beacon_p.add_argument(
        "--webhook-url", default=None,
        help="Public URL where this dashboard's /api/beacon/webhook is reachable",
    )

    unpublish_p = sub.add_parser(
        "unpublish", help="remove a previously published session from the target repo",
    )
    unpublish_p.add_argument("session_id", help="session id to remove")

    parser.add_argument(
        "--deploy-config",
        default=str(REPO_ROOT / "configs" / "deployment.yaml"),
        help="path to deployment.yaml",
    )
    return parser


# -------------------- command handlers --------------------


def cmd_wake(args) -> int:
    from src.client.pod_manager import PodManager, load_deployment
    settings = load_deployment(args.deploy_config)
    mgr = PodManager(settings)
    info = mgr.wake()
    print(json.dumps(info, indent=2))
    return 0


def cmd_sleep(args) -> int:
    from src.client.pod_manager import PodManager, load_deployment
    settings = load_deployment(args.deploy_config)
    mgr = PodManager(settings)
    info = mgr.sleep()
    print(json.dumps(info, indent=2))
    return 0


def cmd_status(args) -> int:
    from src.client.pod_manager import PodManager, load_deployment
    settings = load_deployment(args.deploy_config)
    mgr = PodManager(settings)
    info = mgr.status()
    print(json.dumps(info, indent=2))
    return 0


def cmd_talk(args) -> int:
    import asyncio
    from src.client.audio_bridge import ClientAudioBridge
    from src.client.pod_manager import PodManager, load_deployment

    settings = load_deployment(args.deploy_config)
    mgr = PodManager(settings)
    info = mgr.status()
    if info.get("status") != "RUNNING":
        print(f"pod not running (status={info.get('status')}). run `renee wake` first.")
        return 2
    ip = info.get("public_ip") or ""
    if not ip:
        print("pod has no public IP yet; try again in a few seconds.")
        return 2
    bridge_url = settings.bridge_url_template.format(host=ip)
    print(f"connecting to {bridge_url} ...")
    bridge = ClientAudioBridge(bridge_url)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\ndisconnected.")
    return 0


def cmd_proxy(args) -> int:
    import asyncio

    import yaml

    from src.client.proxy_server import (
        DEFAULT_PROXY_PORT,
        resolve_bridge_url,
        run_proxy,
    )

    # Port precedence: CLI --port > deployment.yaml cloud.proxy_port > default.
    port = args.port
    if port is None:
        try:
            raw = yaml.safe_load(
                Path(args.deploy_config).read_text(encoding="utf-8")
            ) or {}
            port = int((raw.get("cloud") or {}).get("proxy_port", DEFAULT_PROXY_PORT))
        except Exception:
            port = DEFAULT_PROXY_PORT

    if args.bridge_url:
        bridge_url = args.bridge_url
    else:
        try:
            bridge_url = resolve_bridge_url(args.deploy_config)
        except Exception as e:
            print(f"could not resolve bridge URL: {e}", file=sys.stderr)
            return 2
    print(f"bridge: {bridge_url}")

    ssl_context = None
    cert_pem_path = None
    if args.https:
        try:
            from src.client.cert_manager import CERT_NAME, ensure_self_signed_cert
            from src.client.proxy_server import tailscale_ip

            cert_dir = REPO_ROOT / "state" / "certs"
            extra = [ip for ip in [tailscale_ip()] if ip]
            ssl_context = ensure_self_signed_cert(cert_dir, extra_hosts=extra)
            cert_pem_path = cert_dir / CERT_NAME
        except Exception as e:
            print(f"HTTPS setup failed ({e}); falling back to HTTP", file=sys.stderr)
            ssl_context = None
            cert_pem_path = None

    qr_png_path = REPO_ROOT / "state" / "renee_connect_qr.png"
    try:
        asyncio.run(
            run_proxy(
                bridge_url=bridge_url,
                port=port,
                ssl_context=ssl_context,
                cert_path=cert_pem_path,
                qr_png_path=qr_png_path,
            )
        )
    except KeyboardInterrupt:
        print("\nproxy stopped.")
    return 0


def cmd_text(args) -> int:
    # Delegate to the existing M2 chat REPL.
    from src.cli import chat as chat_mod
    return int(chat_mod.main() or 0)


def cmd_eval(args) -> int:
    from src.eval.harness import main as eval_main
    return int(eval_main() or 0)


def cmd_export(args) -> int:
    state_dir = REPO_ROOT / "state"
    dest = Path(args.output)
    dry_run = bool(getattr(args, "dry_run", False))
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for src_path in state_dir.rglob("*"):
        if src_path.is_file():
            rel = src_path.relative_to(state_dir)
            files.append(str(rel))
            if not dry_run:
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(src_path.read_bytes())
    if dry_run:
        print(json.dumps({"dry_run": True, "would_export": files, "destination": str(dest)}, indent=2))
    else:
        print(json.dumps({"copied_files": len(files), "destination": str(dest)}, indent=2))
    return 0


def cmd_check_deps(args) -> int:
    deps = [
        ("websockets", "pip install websockets"),
        ("sounddevice", "pip install sounddevice"),
        ("runpod", "pip install runpod"),
    ]
    missing: list[dict] = []
    for mod, cmd in deps:
        try:
            __import__(mod)
        except ImportError:
            missing.append({"module": mod, "install": cmd})
    if missing:
        print(json.dumps({"missing": missing}, indent=2))
        for m in missing:
            print(f"  {m['module']}: {m['install']}", file=sys.stderr)
        return 1
    print(json.dumps({"missing": []}, indent=2))
    return 0


# -------------------- dispatcher --------------------


def cmd_triage(args) -> int:
    from src.capture.triage import run_triage

    result = run_triage(
        Path(args.session_dir),
        whisper_model=args.whisper_model,
    )
    print(json.dumps({"flag_count": len(result["flags"]), "flags_path": result["flags_path"]}, indent=2))
    return 0


def cmd_highlights(args) -> int:
    from src.capture.review_notes import regenerate_highlights
    from src.capture.session_recorder import default_sessions_root

    root = Path(args.sessions_root) if args.sessions_root else default_sessions_root()
    result = regenerate_highlights(root)
    print(json.dumps(result, indent=2))
    return 0


def cmd_publish(args) -> int:
    from src.capture.publish import PublishError, publish_session
    from src.capture.session_recorder import default_sessions_root

    try:
        result = publish_session(
            default_sessions_root(),
            args.session_id,
            include_audio=args.include_audio,
            confirm=args.confirm,
        )
    except PublishError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_publish_list(args) -> int:
    from src.capture.publish import list_publishable
    from src.capture.session_recorder import default_sessions_root

    rows = list_publishable(default_sessions_root())
    print(json.dumps(rows, indent=2))
    return 0


def cmd_unpublish(args) -> int:
    from src.capture.publish import unpublish_session
    from src.capture.session_recorder import default_sessions_root

    result = unpublish_session(default_sessions_root(), args.session_id)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_dashboard(args) -> int:
    """Open the M15 dashboard in a browser; auto-start it if not running.

    Cheap way to get from "I want to look at mood/health right now" to a
    browser tab without remembering URLs or terminal commands.
    """
    import time
    import urllib.error
    import urllib.request
    import webbrowser

    url = f"http://127.0.0.1:{args.port}"

    def _running() -> bool:
        try:
            with urllib.request.urlopen(f"{url}/api/ping", timeout=1.5) as r:
                return r.status == 200
        except Exception:
            return False

    if _running():
        print(f"dashboard already running at {url}")
    else:
        print(f"dashboard not running; starting it on {url} ...")
        # Spawn detached so we don't block on the foreground; the user's
        # browser will pick up the tab once it serves the first request.
        import subprocess
        subprocess.Popen(
            [sys.executable, "-m", "src.dashboard"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Poll for readiness up to ~5s
        for _ in range(50):
            if _running():
                break
            time.sleep(0.1)
        else:
            print("dashboard didn't come up in 5s; still trying to open the browser")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"could not auto-open browser: {e}")
    return 0


def cmd_logs(args) -> int:
    """Tail the conversation log for a given day, optionally following.

    The conversation logger writes one file per UTC day under
    state/logs/conversations/YYYY-MM-DD.log (orchestrator priority 2).
    This subcommand is the read-side companion: tail the last N lines
    optionally with --follow so you can watch the file grow during a
    live session.
    """
    import time
    import datetime as _dt

    day = args.day or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    log_path = REPO_ROOT / "state" / "logs" / "conversations" / f"{day}.log"

    if not log_path.exists():
        print(f"no log for {day} at {log_path}")
        return 1

    # Print the last N lines first (cheap; conversation logs stay small enough)
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail_n = max(0, int(args.tail))
    for line in lines[-tail_n:]:
        print(line)

    if not args.follow:
        return 0

    # Follow mode: poll mtime + read appended bytes. Pure stdlib so this
    # works without watchdog or platform-specific tail.
    print(f"-- following {log_path} (Ctrl+C to stop) --")
    pos = log_path.stat().st_size
    try:
        while True:
            time.sleep(0.5)
            try:
                size = log_path.stat().st_size
            except FileNotFoundError:
                continue
            if size < pos:
                # File rotated/truncated — start over from the new top
                pos = 0
            if size > pos:
                with log_path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                # chunk may end mid-line — print whole text but no extra newline
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
    except KeyboardInterrupt:
        print()
        return 0


def cmd_migrate_secrets(args) -> int:
    """Copy KNOWN_SECRETS values from env into the OS keyring."""
    from renee import secrets
    summary = secrets.migrate_env_to_keyring()
    if not summary:
        print("nothing to migrate")
        return 0
    width = max(len(k) for k in summary)
    for name, status in summary.items():
        print(f"  {name:<{width}}  {status}")
    return 0


def cmd_version(args) -> int:
    """Print build version + key dependencies. Reads the most-recent git
    commit hash for "build" so a freshly-cloned tree without a tag still
    has a useful identifier."""
    import platform
    import subprocess
    print(f"renee   {_renee_version()}")
    print(f"python  {platform.python_version()}")
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        print(f"git     {head} ({branch})")
    except Exception:
        print("git     (not a git repo)")
    # Optional deps — surface for support purposes
    for name in ("paramiko", "websockets", "runpod", "yaml", "fastapi"):
        try:
            mod = __import__(name)
            ver = getattr(mod, "__version__", "?")
            print(f"  {name:<12} {ver}")
        except ImportError:
            print(f"  {name:<12} (not installed)")
    return 0


def _renee_version() -> str:
    """Read the version string. Prefers a VERSION file at the repo root;
    falls back to the package's __version__ if present, else 'dev'."""
    vf = REPO_ROOT / "VERSION"
    if vf.exists():
        return vf.read_text(encoding="utf-8").strip()
    try:
        import renee
        return getattr(renee, "__version__", "dev")
    except Exception:
        return "dev"


def cmd_sessions(args) -> int:
    """List captured sessions in a fixed-width table.

    Columns: id, start time (HH:MM), duration (min), turn count,
    presence score, public flag. Filter to one UTC day with --day; the
    default lists every session under the sessions root.
    """
    import json as _json
    from src.capture.session_recorder import default_sessions_root
    root = Path(args.sessions_root) if args.sessions_root else default_sessions_root()
    if not root.exists():
        print(f"sessions root not found: {root}")
        return 0
    rows: list[dict] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        manifest_path = d / "session_manifest.json"
        if not manifest_path.exists():
            continue
        try:
            m = _json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if args.day:
            start = (m.get("start_time") or "")
            if not start.startswith(args.day):
                continue
        # Duration: prefer start/end times; fall back to manifest field
        try:
            from datetime import datetime
            s = datetime.fromisoformat(m.get("start_time", "").replace("Z", ""))
            e_raw = m.get("end_time", "")
            if e_raw:
                e = datetime.fromisoformat(e_raw.replace("Z", ""))
                duration_min = round((e - s).total_seconds() / 60.0, 1)
            else:
                duration_min = "?"
        except Exception:
            duration_min = m.get("duration_minutes", "?")
        # Transcript turn count
        turns: int | str = "?"
        tpath = d / "transcript.json"
        if tpath.exists():
            try:
                events = _json.loads(tpath.read_text(encoding="utf-8"))
                if isinstance(events, list):
                    turns = len(events)
            except Exception:
                pass
        rows.append({
            "id": d.name,
            "start": (m.get("start_time") or "")[:16].replace("T", " "),
            "duration": duration_min,
            "turns": turns,
            "score": m.get("presence_score") if m.get("presence_score") is not None else "-",
            "public": "y" if m.get("public") else "n",
            "published": "y" if m.get("github_published") else "n",
        })
    if not rows:
        print("no sessions found")
        return 0
    # Print fixed-width
    print(f"{'id':<22} {'start':<16} {'min':>5} {'turns':>5} {'score':>5} {'pub':>4} {'pushed':>6}")
    print("-" * 72)
    for r in rows:
        print(
            f"{r['id']:<22} {r['start']:<16} "
            f"{str(r['duration']):>5} {str(r['turns']):>5} "
            f"{str(r['score']):>5} {r['public']:>4} {r['published']:>6}"
        )
    print(f"\n{len(rows)} session(s)")
    return 0


def cmd_report(args) -> int:
    """Generate a Markdown report for a captured session."""
    from src.capture.report import gather, render, write_report
    from src.capture.session_recorder import default_sessions_root
    root = Path(args.sessions_root) if args.sessions_root else default_sessions_root()
    session_dir = root / args.session_id
    if not session_dir.exists():
        print(f"session not found: {session_dir}")
        return 1
    if args.print_only:
        print(render(gather(session_dir)))
    else:
        out = write_report(session_dir)
        print(f"wrote {out}")
    return 0


def cmd_fetch_logs(args) -> int:
    """Pull conversation logs from the pod's /workspace/state/logs/
    conversations/ to the OptiPlex's state/logs/conversations/.

    Uses paramiko SFTP. Skipped silently if paramiko isn't installed —
    the conversation log is also tailable via `renee logs` once it's
    been written locally (after manual scp, etc.).
    """
    try:
        import paramiko
    except ImportError:
        print("paramiko not installed; pip install paramiko then re-run")
        return 2

    from src.client.pod_manager import load_deployment, PodManager
    settings = load_deployment(args.deploy_config)
    if not settings.pod_id:
        print("no pod_id in deployment.yaml")
        return 2

    rp = PodManager(settings)._client()
    pod = rp.get_pod(settings.pod_id) or {}
    runtime = pod.get("runtime") or {}
    ssh_host = ""
    ssh_port = None
    for port in runtime.get("ports") or []:
        if port.get("isIpPublic") and port.get("privatePort") == 22:
            ssh_host = port["ip"]
            ssh_port = port["publicPort"]
            break
    if not ssh_host or ssh_port is None:
        print("pod has no public SSH port (expose 22 in TCP port map)")
        return 2

    key_path = Path(args.ssh_key or os.environ.get("RENEE_POD_SSH_KEY", str(Path.home() / ".ssh" / "id_rsa")))
    if not key_path.exists():
        print(f"SSH key not found: {key_path}")
        return 2

    dest = Path(args.dest) if args.dest else (REPO_ROOT / "state" / "logs" / "conversations")
    dest.mkdir(parents=True, exist_ok=True)

    print(f"connecting to root@{ssh_host}:{ssh_port} ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ssh_host, port=int(ssh_port), username="root",
                       key_filename=str(key_path), timeout=10)
    except Exception as e:
        print(f"ssh connect failed: {e}")
        return 2

    try:
        sftp = client.open_sftp()
        remote = "/workspace/state/logs/conversations"
        try:
            files = sftp.listdir(remote)
        except IOError:
            print(f"no logs at {remote} on pod")
            return 0
        copied = 0
        for fname in files:
            if not fname.endswith(".log"):
                continue
            local = dest / fname
            sftp.get(f"{remote}/{fname}", str(local))
            copied += 1
            print(f"  pulled {fname}")
        sftp.close()
        print(f"\n{copied} log file(s) -> {dest}")
    finally:
        client.close()
    return 0


def cmd_backup(args) -> int:
    """Run a one-shot backup. Thin wrapper over scripts/run_backup.main()."""
    import importlib.util
    setup_path = REPO_ROOT / "scripts" / "run_backup.py"
    spec = importlib.util.spec_from_file_location("renee_run_backup", setup_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    argv: list[str] = []
    if args.check:
        argv.append("--check")
    if args.force:
        argv.append("--force")
    return mod.main(argv)


def cmd_preflight(args) -> int:
    """Run the launcher's pre-flight gates (tailscale, pod status, beacon,
    daily cap) without actually starting a session. Useful as a quick
    "is everything ready?" check before tonight's run.

    Reads the same checks the launcher does so they stay in sync. Exits
    0 when all gates pass, 1 when at least one fails.
    """
    import importlib.util
    setup_path = REPO_ROOT / "scripts" / "session_launcher.py"
    spec = importlib.util.spec_from_file_location("session_launcher", setup_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    failures: list[str] = []

    print("[1/4] Tailscale ...")
    ok, info = mod._check_tailscale()
    if ok:
        print(f"      ok ({info})")
    else:
        print(f"      FAIL: {info}")
        failures.append("tailscale")

    print("[2/4] RunPod status ...")
    pod_ok, pod_info = mod._check_pod()
    if pod_ok:
        print(f"      ok (pod_id={pod_info.get('id')} ip={pod_info.get('public_ip')})")
    else:
        print(f"      FAIL: {pod_info}")
        failures.append("pod")

    print("[3/4] Beacon (optional) ...")
    beacon_warn = mod._check_beacon()
    if beacon_warn is None:
        url = os.environ.get("BEACON_URL", "").strip()
        print(f"      ok ({url})" if url else "      skipped (BEACON_URL not set)")
    else:
        print(f"      WARN: {beacon_warn}")
        # Beacon is soft — not a failure.

    print("[4/4] Daily cap ...")
    cap = mod._check_daily_cap()
    if cap is None:
        print("      skipped (no health monitor / cap configured)")
    else:
        rem = cap["remaining_minutes"]
        flag = "" if rem > 30 else (" [low]" if rem > 0 else " [CAP REACHED]")
        print(f"      {cap['used_minutes']:.0f} of {cap['cap_minutes']:.0f} min used; "
              f"{rem:.0f} min remaining{flag}")
        if rem <= 0:
            failures.append("daily-cap-reached")

    if failures:
        print(f"\nNOT READY: {', '.join(failures)}")
        return 1
    print("\nAll checks passed.")
    return 0


def cmd_beacon_setup(args) -> int:
    """Fetch Beacon's public key + optionally PATCH the agent's webhook_url.

    Two outputs depending on flags:
      1. Always writes state/beacon_public_key.b64 with the fetched key so
         the receiver can verify webhooks signed by this beacon.
      2. If --agent-id + --webhook-url given, also PATCHes
         /v1/agents/{agent_id} so Beacon knows where to deliver death
         certs. Requires --api-key (or BEACON_API_KEY env).
    """
    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue

    base = args.url.rstrip("/")
    state_dir = REPO_ROOT / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch + persist the public key
    print(f"fetching public key from {base}/v1/server/public-key ...")
    try:
        with _ur.urlopen(f"{base}/v1/server/public-key", timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except _ue.URLError as e:
        print(f"error: {e}")
        return 2
    pub = data.get("public_key") or data.get("server_public_key")
    if not pub:
        print(f"error: no public_key in response: {data}")
        return 2
    out = state_dir / "beacon_public_key.b64"
    out.write_text(str(pub).strip() + "\n", encoding="utf-8")
    print(f"  wrote {out}")
    print(f"  set BEACON_PUBLIC_KEY in .env if you'd rather override this file:")
    print(f"      BEACON_PUBLIC_KEY={pub}")

    # 2. Optionally register the webhook on the agent
    if args.agent_id and args.webhook_url:
        api_key = args.api_key or os.environ.get("BEACON_API_KEY", "")
        if not api_key:
            print("error: --api-key (or BEACON_API_KEY) required to PATCH webhook_url")
            return 2
        body = _json.dumps({"webhook_url": args.webhook_url}).encode("utf-8")
        req = _ur.Request(
            f"{base}/v1/agents/{args.agent_id}",
            data=body, method="PATCH",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with _ur.urlopen(req, timeout=5) as resp:
                response = _json.loads(resp.read().decode("utf-8"))
        except _ue.URLError as e:
            print(f"error patching webhook: {e}")
            return 2
        print(f"  registered webhook_url={response.get('webhook_url')}")
    elif args.agent_id or args.webhook_url:
        print("note: provide both --agent-id and --webhook-url to register webhook")

    return 0


HANDLERS = {
    "wake": cmd_wake,
    "talk": cmd_talk,
    "proxy": cmd_proxy,
    "sleep": cmd_sleep,
    "status": cmd_status,
    "text": cmd_text,
    "eval": cmd_eval,
    "export": cmd_export,
    "check-deps": cmd_check_deps,
    "triage": cmd_triage,
    "highlights": cmd_highlights,
    "publish": cmd_publish,
    "publish-list": cmd_publish_list,
    "unpublish": cmd_unpublish,
    "dashboard": cmd_dashboard,
    "logs": cmd_logs,
    "migrate-secrets": cmd_migrate_secrets,
    "beacon-setup": cmd_beacon_setup,
    "backup": cmd_backup,
    "preflight": cmd_preflight,
    "version": cmd_version,
    "fetch-logs": cmd_fetch_logs,
    "report": cmd_report,
    "sessions": cmd_sessions,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    _emit_encryption_warning()
    handler = HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
