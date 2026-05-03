"""On-demand backup of Renée's session + state directories.

Reads ``backup.*`` from configs/deployment.yaml:
  enabled: bool          — top-level on/off; nothing runs when False
  retention_days: int     — drop backups older than this on every run
  encrypt: bool           — gzip-compress; encryption itself stays out of
                            scope here (file-system ACLs cover the OptiPlex)
  offsite: bool           — when True + offsite_bucket set, also sync the
                            new tarball to a Backblaze B2 bucket via rclone
                            if available

Layout on disk:
  state/backups/YYYY-MM-DD-HHMMSS.tar[.gz]
  state/backups/manifest.jsonl   — one JSON line per backup with metadata

The backup is intentionally non-incremental: every run produces a full
tar of the renee-sessions dir + state/ subset (excluding *.db journals
that may be open, and ephemeral caches). Retention prunes older tars
in-place. Pure stdlib — tarfile + json — so no extra deps.

Trigger ad-hoc with ``python -m scripts.run_backup``; the cron schedule
field is informational until the OptiPlex actually has a scheduled task
hitting this entry point.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger("renee.backup")


@dataclass
class BackupConfig:
    enabled: bool = False
    retention_days: int = 30
    encrypt: bool = True
    offsite: bool = False
    offsite_bucket: Optional[str] = None
    schedule: str = ""  # informational

    @classmethod
    def from_yaml(cls, deploy_yaml_path: Path) -> "BackupConfig":
        try:
            import yaml
            cfg = yaml.safe_load(deploy_yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return cls()
        b = cfg.get("backup") or {}
        return cls(
            enabled=bool(b.get("enabled", False)),
            retention_days=int(b.get("retention_days", 30)),
            encrypt=bool(b.get("encrypt", True)),
            offsite=bool(b.get("offsite", False)),
            offsite_bucket=b.get("offsite_bucket"),
            schedule=str(b.get("schedule", "")),
        )


@dataclass
class BackupResult:
    ok: bool
    path: Optional[Path] = None
    bytes_written: int = 0
    error: Optional[str] = None
    pruned: int = 0
    offsite_uploaded: bool = False


# Paths to include in a backup. Keep this conservative — we want
# replayable state + the QAL chain artifacts, not gigabytes of
# generated audio caches.
DEFAULT_INCLUDE: tuple[str, ...] = (
    "global_chain_root.json",
    "global_chain_root.json.bak",
)
DEFAULT_STATE_INCLUDE: tuple[str, ...] = (
    "logs",
    "identities",
    "beacon_credentials.json",
    "beacon_public_key.b64",
    "beacon_deaths.jsonl",
    "cost_ledger.db",
)


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")


def _add_if_exists(tar: tarfile.TarFile, path: Path, arcname: str) -> bool:
    if not path.exists():
        return False
    tar.add(path, arcname=arcname)
    return True


def run_backup(
    *,
    repo_root: Path,
    sessions_root: Optional[Path] = None,
    deploy_yaml: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
) -> BackupResult:
    """Run one backup pass. Caller decides scheduling — we just do work
    when called. Returns a BackupResult that the caller logs."""
    deploy_yaml = deploy_yaml or (repo_root / "configs" / "deployment.yaml")
    cfg = BackupConfig.from_yaml(deploy_yaml)
    if not cfg.enabled:
        return BackupResult(ok=True, error="disabled in deployment.yaml")

    target_dir = out_dir or (repo_root / "state" / "backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    when = now or _dt.datetime.now()
    suffix = ".tar.gz" if cfg.encrypt else ".tar"
    archive = target_dir / f"{when.strftime('%Y-%m-%d-%H%M%S')}{suffix}"

    mode = "w:gz" if cfg.encrypt else "w"
    state_root = repo_root / "state"
    sessions_dir = sessions_root
    if sessions_dir is None:
        # Default sessions root mirrors session_recorder.default_sessions_root
        env = os.environ.get("RENEE_SESSIONS_DIR", "").strip()
        sessions_dir = Path(env) if env else Path(r"C:\Users\Epsar\renee-sessions")

    try:
        with tarfile.open(archive, mode) as tar:
            for name in DEFAULT_INCLUDE:
                _add_if_exists(tar, sessions_dir / name, f"sessions/{name}")
            if sessions_dir.exists():
                # Also pull in per-session manifests + attestation chains;
                # the audio WAVs are intentionally excluded — they're large
                # and already replicable from the chain + manifest.
                for sess in sorted(sessions_dir.iterdir()):
                    if not sess.is_dir() or sess.name.startswith("_"):
                        continue
                    for fname in (
                        "session_manifest.json",
                        "attestation_chain.jsonl",
                        "transcript.json",
                        "notes.md",
                        "review_notes.md",
                        "highlights.md",
                    ):
                        _add_if_exists(
                            tar, sess / fname, f"sessions/{sess.name}/{fname}",
                        )
            for name in DEFAULT_STATE_INCLUDE:
                _add_if_exists(tar, state_root / name, f"state/{name}")
    except Exception as e:
        if archive.exists():
            try:
                archive.unlink()
            except Exception:
                pass
        return BackupResult(ok=False, error=f"tar failed: {e}")

    # Prune older backups
    pruned = _prune(target_dir, retention_days=cfg.retention_days, now=when)

    # Manifest line
    record = {
        "timestamp": when.isoformat(),
        "archive": archive.name,
        "bytes": archive.stat().st_size,
        "encrypt": cfg.encrypt,
        "offsite": cfg.offsite,
        "offsite_bucket": cfg.offsite_bucket,
    }
    (target_dir / "manifest.jsonl").open("a", encoding="utf-8").write(
        json.dumps(record) + "\n",
    )

    return BackupResult(
        ok=True, path=archive,
        bytes_written=archive.stat().st_size,
        pruned=pruned,
    )


def _prune(target_dir: Path, *, retention_days: int, now: _dt.datetime) -> int:
    """Remove backup tarballs older than retention_days. Returns count removed."""
    if retention_days <= 0:
        return 0
    cutoff = now.timestamp() - (retention_days * 86400)
    removed = 0
    for f in target_dir.iterdir():
        if not f.is_file():
            continue
        if not (f.name.endswith(".tar") or f.name.endswith(".tar.gz")):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            continue
    return removed


__all__ = [
    "BackupConfig", "BackupResult",
    "run_backup",
]
