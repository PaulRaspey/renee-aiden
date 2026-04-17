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
import sys
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="renee", description="Talk to Renée.")
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("wake", help="start the cloud pod")
    sub.add_parser("talk", help="open audio bridge to the running pod")
    sub.add_parser("sleep", help="graceful pod shutdown")
    sub.add_parser("status", help="show pod status")
    sub.add_parser("text", help="local text-mode REPL")
    sub.add_parser("eval", help="run the eval harness")

    export_p = sub.add_parser("export", help="export state to a directory")
    export_p.add_argument(
        "--output", default=str(REPO_ROOT / "exports"),
        help="destination directory",
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
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src_path in state_dir.rglob("*"):
        if src_path.is_file():
            rel = src_path.relative_to(state_dir)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(src_path.read_bytes())
            copied += 1
    print(json.dumps({"copied_files": copied, "destination": str(dest)}, indent=2))
    return 0


# -------------------- dispatcher --------------------


HANDLERS = {
    "wake": cmd_wake,
    "talk": cmd_talk,
    "sleep": cmd_sleep,
    "status": cmd_status,
    "text": cmd_text,
    "eval": cmd_eval,
    "export": cmd_export,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handler = HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
