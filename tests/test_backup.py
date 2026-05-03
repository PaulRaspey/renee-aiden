"""Tests for src.client.backup (#55)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import tarfile
import time
from pathlib import Path

import pytest

from src.client.backup import BackupConfig, BackupResult, run_backup


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _make_minimal_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Return (repo_root, sessions_root) populated with a few files."""
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    (repo / "configs").mkdir(parents=True)
    (repo / "state" / "logs").mkdir(parents=True)
    sessions.mkdir(parents=True)

    _write_yaml(
        repo / "configs" / "deployment.yaml",
        "backup:\n"
        "  enabled: true\n"
        "  retention_days: 30\n"
        "  encrypt: true\n",
    )
    (sessions / "global_chain_root.json").write_text(
        json.dumps({"genesis": True}), encoding="utf-8",
    )
    s1 = sessions / "2026-05-03"
    s1.mkdir()
    (s1 / "session_manifest.json").write_text("{}", encoding="utf-8")
    (s1 / "transcript.json").write_text("[]", encoding="utf-8")
    # WAVs should NOT be included
    (s1 / "mic.wav").write_bytes(b"\x00" * 1024)

    (repo / "state" / "logs" / "marker.txt").write_text("hi", encoding="utf-8")

    return repo, sessions


def test_backup_disabled_returns_ok_and_does_nothing(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    _write_yaml(
        repo / "configs" / "deployment.yaml",
        "backup:\n  enabled: false\n",
    )
    result = run_backup(repo_root=repo)
    assert result.ok is True
    assert result.path is None  # no archive written


def test_backup_writes_archive_and_records_manifest(tmp_path: Path):
    repo, sessions = _make_minimal_repo(tmp_path)
    result = run_backup(repo_root=repo, sessions_root=sessions)
    assert result.ok is True
    assert result.path is not None
    assert result.path.exists()
    assert result.path.suffix == ".gz"
    assert result.bytes_written > 0
    # Manifest got a line
    manifest = repo / "state" / "backups" / "manifest.jsonl"
    assert manifest.exists()
    rec = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    assert rec["archive"] == result.path.name


def test_backup_excludes_wav_files(tmp_path: Path):
    """WAVs are large + replicable from the chain — never archive them."""
    repo, sessions = _make_minimal_repo(tmp_path)
    result = run_backup(repo_root=repo, sessions_root=sessions)
    with tarfile.open(result.path, "r:gz") as tar:
        names = tar.getnames()
    assert any("session_manifest.json" in n for n in names)
    assert not any(n.endswith(".wav") for n in names)


def test_backup_includes_chain_root_at_top(tmp_path: Path):
    repo, sessions = _make_minimal_repo(tmp_path)
    result = run_backup(repo_root=repo, sessions_root=sessions)
    with tarfile.open(result.path, "r:gz") as tar:
        names = tar.getnames()
    assert any(n == "sessions/global_chain_root.json" for n in names)


def test_backup_uncompressed_when_encrypt_false(tmp_path: Path):
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (repo / "configs").mkdir(parents=True)
    _write_yaml(
        repo / "configs" / "deployment.yaml",
        "backup:\n  enabled: true\n  encrypt: false\n",
    )
    result = run_backup(repo_root=repo, sessions_root=sessions)
    assert result.ok is True
    assert result.path.suffix == ".tar"
    # tarfile reads uncompressed tarballs without a mode prefix
    with tarfile.open(result.path, "r") as tar:
        # No payload but it's a valid tar
        assert tar is not None


def test_backup_prune_drops_old_archives(tmp_path: Path):
    repo, sessions = _make_minimal_repo(tmp_path)
    backups = repo / "state" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    # Plant an "old" tarball with mtime 60 days ago
    old = backups / "ancient.tar.gz"
    old.write_bytes(b"old")
    sixty_days_ago = time.time() - 60 * 86400
    os.utime(old, (sixty_days_ago, sixty_days_ago))
    # Plant a "recent" one (1 day ago)
    recent = backups / "recent.tar.gz"
    recent.write_bytes(b"recent")
    one_day_ago = time.time() - 86400
    os.utime(recent, (one_day_ago, one_day_ago))

    result = run_backup(repo_root=repo, sessions_root=sessions)
    assert result.ok is True
    assert result.pruned >= 1
    assert not old.exists()       # 60 days > 30 day retention
    assert recent.exists()        # 1 day < 30 day retention


def test_backup_config_from_yaml_defaults():
    """Missing backup block → safe defaults (enabled=False)."""
    p = Path("/no/such/file.yaml")
    cfg = BackupConfig.from_yaml(p)
    assert cfg.enabled is False
    assert cfg.retention_days == 30


def test_backup_handles_missing_sessions_dir(tmp_path: Path):
    """No sessions captured yet — backup should still produce an archive
    of state/ alone without crashing."""
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "state" / "logs").mkdir(parents=True)
    _write_yaml(
        repo / "configs" / "deployment.yaml",
        "backup:\n  enabled: true\n",
    )
    sessions = tmp_path / "missing_sessions"  # never created
    result = run_backup(repo_root=repo, sessions_root=sessions)
    assert result.ok is True
    assert result.path is not None
    assert result.path.exists()
