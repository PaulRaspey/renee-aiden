"""
scripts/volume_setup.py — one-shot RunPod workspace provisioning.

Run locally from the OptiPlex before the first `renee wake`:
    python scripts/volume_setup.py

What it does:
    1. Reads pod_id from configs/deployment.yaml
    2. Looks up the pod's public SSH endpoint via the RunPod API
    3. Copies the repo (minus venv/state/generated assets) into /workspace
    4. Installs pip requirements on the pod
    5. Downloads model artifacts listed under cloud.model_repos
       into /workspace/models/
    6. Creates /workspace/state/ directory structure
    7. Verifies each step and prints a summary

IMPORTANT: port 8765 (audio bridge) must be in the pod's TCP port
mapping BEFORE `renee wake`. RunPod cannot add exposed ports to a
running/existing pod — if the current pod was created without 8765
exposed, recreate the pod via the RunPod UI (Pods → Deploy → expose
8765 as TCP), update configs/deployment.yaml with the new pod_id,
then run this script again.

SSH auth:
    Uses ~/.ssh/id_rsa by default. Override with the RENEE_POD_SSH_KEY
    env var. The pod must be created with the matching public key in
    its PUBLIC_KEY env var (RunPod console → pod → edit → env vars).
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path

import yaml

try:
    import paramiko
except ImportError:
    print("paramiko required. pip install paramiko", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


REPO_ROOT = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env")


# Dirs/files on the OptiPlex that should NOT be copied to the pod.
COPY_EXCLUDES = {
    ".venv", ".git", "__pycache__", ".pytest_cache",
    "state", "paralinguistics", "voices", "exports",
    ".env",  # secrets go to the pod via its own env vars
}

# State subdirectories to create on the pod.
STATE_DIRS = [
    "state",
    "state/eval_runs",
    "state/backups",
]


# ---------------------------------------------------------------------------
# pod lookup
# ---------------------------------------------------------------------------


def _load_pod_ssh_target(pod_id: str) -> tuple[str, int]:
    import runpod
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        raise RuntimeError("RUNPOD_API_KEY not in environment (put it in .env).")
    runpod.api_key = api_key
    pod = runpod.get_pod(pod_id) or {}

    # Preflight: pod must have a non-null PUBLIC_KEY or SSH auth will fail
    # silently mid-run (after we've started copying). pod.env is a list of
    # "KEY=VALUE" strings.
    pod_env: dict[str, str] = {}
    for entry in pod.get("env") or []:
        if "=" in entry:
            k, _, v = entry.partition("=")
            pod_env[k] = v
    public_key = pod_env.get("PUBLIC_KEY", "")
    if not public_key or public_key == "null":
        raise SystemExit(
            f"Pod {pod_id} has PUBLIC_KEY={public_key!r}. Recreate the pod "
            "with your SSH public key set in the PUBLIC_KEY env var before "
            "running this script (RunPod console → pod → edit → env vars)."
        )

    runtime = pod.get("runtime") or {}
    for port in runtime.get("ports") or []:
        if port.get("isIpPublic") and port.get("privatePort") == 22:
            return port["ip"], port["publicPort"]
    raise RuntimeError(
        f"Pod {pod_id} has no public SSH port. Is it RUNNING with port 22 "
        f"exposed? current ports: {runtime.get('ports')}"
    )


# ---------------------------------------------------------------------------
# ssh helpers
# ---------------------------------------------------------------------------


def _connect_ssh(host: str, port: int, key_path: Path) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username="root",
        key_filename=str(key_path),
        timeout=30,
    )
    return client


def _run(client: paramiko.SSHClient, cmd: str, *, stream: bool = True) -> int:
    """Run a command on the pod. Streams stdout/stderr. Returns exit code."""
    _, stdout, stderr = client.exec_command(cmd, get_pty=True)
    if stream:
        for line in iter(stdout.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
    rc = stdout.channel.recv_exit_status()
    if not stream:
        err = stderr.read().decode(errors="replace").strip()
        if err:
            print(err, file=sys.stderr)
    return rc


def _sftp_mkdir_p(sftp, remote_path: str) -> None:
    parts = remote_path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}"
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def _sftp_put_tree(client: paramiko.SSHClient, local: Path, remote: str) -> int:
    count = 0
    sftp = client.open_sftp()
    try:
        _sftp_mkdir_p(sftp, remote)
        for root, dirs, files in os.walk(local):
            dirs[:] = [d for d in dirs if d not in COPY_EXCLUDES]
            rel = Path(root).relative_to(local).as_posix()
            if rel != "." and any(part in COPY_EXCLUDES for part in Path(rel).parts):
                continue
            remote_root = f"{remote}/{rel}" if rel != "." else remote
            _sftp_mkdir_p(sftp, remote_root)
            for f in files:
                if f in COPY_EXCLUDES:
                    continue
                sftp.put(str(Path(root) / f), f"{remote_root}/{f}")
                count += 1
    finally:
        sftp.close()
    return count


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


def step_copy_repo(client, repo_root: Path) -> int:
    print(">>> step 1/5: copy repo to /workspace", flush=True)
    count = _sftp_put_tree(client, repo_root, "/workspace")
    print(f"    copied {count} files\n", flush=True)
    return count


def step_install_requirements(client) -> int:
    print(">>> step 2/5: pip install -r requirements.txt", flush=True)
    rc = _run(
        client,
        "cd /workspace && python -m pip install --upgrade -r requirements.txt",
    )
    print(f"    pip exit code: {rc}\n", flush=True)
    return rc


def step_download_models(client, model_repos: list[dict]) -> list[tuple[str, int]]:
    print(">>> step 3/5: download model artifacts", flush=True)
    if not model_repos:
        print("    cloud.model_repos is empty in deployment.yaml — skipping", flush=True)
        return []
    results: list[tuple[str, int]] = []
    for entry in model_repos:
        repo_id = entry["repo_id"]
        local_name = entry.get("local_name") or repo_id.split("/")[-1]
        target = f"/workspace/models/{local_name}"
        print(f"    {repo_id} -> {target}", flush=True)
        cmd = (
            f"mkdir -p {target} && "
            f"python -c \"from huggingface_hub import snapshot_download; "
            f"snapshot_download(repo_id='{repo_id}', local_dir='{target}')\""
        )
        rc = _run(client, cmd)
        results.append((repo_id, rc))
    print()
    return results


def step_create_state_dirs(client) -> int:
    print(">>> step 4/5: create /workspace/state/ structure", flush=True)
    cmd = "mkdir -p " + " ".join(f"/workspace/{d}" for d in STATE_DIRS)
    rc = _run(client, cmd, stream=False)
    print(f"    mkdir exit: {rc}\n", flush=True)
    return rc


def step_verify(client, model_repos: list[dict]) -> dict[str, int]:
    print(">>> step 5/5: verify", flush=True)
    checks: dict[str, str] = {
        "repo": "test -f /workspace/requirements.txt",
        "torch": "python -c 'import torch; print(torch.__version__)'",
        "huggingface_hub": "python -c 'import huggingface_hub; print(huggingface_hub.__version__)'",
        "state_dir": "test -d /workspace/state",
    }
    for entry in model_repos or []:
        name = entry.get("local_name") or entry["repo_id"].split("/")[-1]
        checks[f"model:{name}"] = f"test -d /workspace/models/{name} && ls /workspace/models/{name} | head -1"
    results: dict[str, int] = {}
    for name, cmd in checks.items():
        rc = _run(client, cmd, stream=False)
        print(f"    {name}: {'ok' if rc == 0 else f'FAIL(rc={rc})'}", flush=True)
        results[name] = rc
    return results


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision a fresh RunPod pod for Renée.")
    parser.add_argument("--deploy-config", default=str(REPO_ROOT / "configs" / "deployment.yaml"))
    parser.add_argument(
        "--ssh-key",
        default=os.environ.get("RENEE_POD_SSH_KEY", str(Path.home() / ".ssh" / "id_rsa")),
    )
    # Honor an explicit argv list so callers (e.g. pod_manager._default_volume_setup_runner)
    # can invoke this in-process without inheriting the parent's command-line flags.
    args = parser.parse_args(argv)

    cfg = yaml.safe_load(Path(args.deploy_config).read_text(encoding="utf-8")) or {}
    cloud = cfg.get("cloud") or {}
    pod_id = cloud.get("pod_id", "")
    model_repos = cloud.get("model_repos") or []
    if not pod_id:
        raise SystemExit("No cloud.pod_id in deployment.yaml")

    key_path = Path(args.ssh_key)
    if not key_path.exists():
        raise SystemExit(f"SSH key not found: {key_path}. Set RENEE_POD_SSH_KEY.")

    print(f"Looking up SSH endpoint for pod {pod_id} ...", flush=True)
    host, port = _load_pod_ssh_target(pod_id)
    print(f"    ssh root@{host}:{port}\n", flush=True)

    print(f"Connecting with key {key_path} ...", flush=True)
    client = _connect_ssh(host, port, key_path)
    try:
        step_copy_repo(client, REPO_ROOT)
        step_install_requirements(client)
        step_download_models(client, model_repos)
        step_create_state_dirs(client)
        results = step_verify(client, model_repos)
    finally:
        client.close()

    failures = [k for k, rc in results.items() if rc != 0]
    print()
    print("=" * 70)
    print(f"Provisioning done. {len(results) - len(failures)}/{len(results)} checks passed.")
    if failures:
        print(f"FAILURES: {failures}")
    print("=" * 70)
    print(textwrap.dedent("""
        REMINDER: port 8765 (audio bridge) must be in the pod's TCP port
        mapping BEFORE `renee wake` can hand out a working bridge URL.
        If the current pod was created without 8765 exposed, recreate the
        pod via the RunPod UI, update configs/deployment.yaml with the new
        pod_id, re-run this script against the new pod, then `renee wake`.
    """).strip())

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
