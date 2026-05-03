"""One-shot backup runner.

Usage::

    python scripts/run_backup.py            # respects backup.enabled
    python scripts/run_backup.py --force    # ignore enabled flag
    python scripts/run_backup.py --check    # report what's already there

Intended trigger: Windows Task Scheduler (cron-equivalent on Windows)
calling this script daily, matching backup.schedule in deployment.yaml.
The script itself is idempotent — multiple runs in the same minute
produce different timestamped archives, so it's safe to wire to a
nightly OS-level scheduler.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_backup")
    parser.add_argument("--force", action="store_true",
                        help="Ignore backup.enabled and run anyway")
    parser.add_argument("--check", action="store_true",
                        help="List existing backups + manifest tail; do nothing else")
    args = parser.parse_args(argv)

    backups_dir = REPO_ROOT / "state" / "backups"

    if args.check:
        if not backups_dir.exists():
            print(f"no backups at {backups_dir}")
            return 0
        files = sorted(p for p in backups_dir.iterdir()
                       if p.is_file() and (p.suffix == ".gz" or p.suffix == ".tar"))
        for f in files:
            sz = f.stat().st_size
            print(f"  {f.name}  ({sz/1024/1024:.1f} MiB)")
        manifest = backups_dir / "manifest.jsonl"
        if manifest.exists():
            print(f"\nmanifest: {manifest}")
            for line in manifest.read_text(encoding="utf-8").splitlines()[-5:]:
                try:
                    rec = json.loads(line)
                    print(f"  {rec.get('timestamp', '?')}  {rec.get('archive', '?')}  "
                          f"({rec.get('bytes', 0)/1024/1024:.1f} MiB)")
                except Exception:
                    pass
        return 0

    from src.client.backup import BackupConfig, run_backup
    cfg = BackupConfig.from_yaml(REPO_ROOT / "configs" / "deployment.yaml")
    if not cfg.enabled and not args.force:
        print("backup disabled in deployment.yaml; use --force to run anyway")
        return 0

    if args.force and not cfg.enabled:
        # Override the config flag for this run only
        cfg_yaml = REPO_ROOT / "configs" / "deployment.yaml"
        # We re-call run_backup but flip enabled in the config branch
        # by passing a temp yaml. Simpler: re-implement the small dispatch
        # inline against the existing config object.
        from src.client.backup import run_backup as _run
        # The function reads config from yaml — hack the env: write a temp
        # yaml file with enabled:true and point at it. Cleaner: just call
        # run_backup with a deploy_yaml override pointing at a synth file.
        synth = REPO_ROOT / "state" / "_force_backup_cfg.yaml"
        synth.parent.mkdir(parents=True, exist_ok=True)
        synth.write_text(
            "backup:\n"
            f"  enabled: true\n"
            f"  retention_days: {cfg.retention_days}\n"
            f"  encrypt: {str(cfg.encrypt).lower()}\n",
            encoding="utf-8",
        )
        result = _run(repo_root=REPO_ROOT, deploy_yaml=synth)
        try:
            synth.unlink()
        except Exception:
            pass
    else:
        result = run_backup(repo_root=REPO_ROOT)

    if not result.ok:
        print(f"backup failed: {result.error}")
        return 1
    if result.path is None:
        # Disabled or no-op path
        print(result.error or "ok")
        return 0
    print(f"  archive: {result.path}")
    print(f"  bytes:   {result.bytes_written}")
    print(f"  pruned:  {result.pruned} older backup(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
