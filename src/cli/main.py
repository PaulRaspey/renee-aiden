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
