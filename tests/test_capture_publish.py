"""Tests for the selective GitHub publishing pipeline (Feature 6).

Hard-gates (public flag, presence_score), staging contents, redaction,
audio exclusion by default, Opus-only audio inclusion, confirm gating,
unpublish flow, and chain manifest continuity. All git and encode
side effects are injected and captured; nothing is actually pushed and
ffmpeg is never invoked.
"""
from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from src.capture import publish as pub
from src.capture.publish import (
    PublishConfig,
    PublishError,
    list_publishable,
    publish_session,
    stage_session,
    unpublish_session,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(bytes(48000 * 2))  # 1 second silence


@dataclass
class _FakeGitResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _GitRecorder:
    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[list[str], Path]] = []
        self.returncode = returncode

    def __call__(self, args, cwd):
        self.calls.append((list(args), Path(cwd)))
        return _FakeGitResult(returncode=self.returncode)


class _EncodeRecorder:
    def __init__(self):
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, wav_path: Path, opus_path: Path) -> None:
        self.calls.append((Path(wav_path), Path(opus_path)))
        opus_path.write_bytes(b"OPUS-FAKE")


def _write_chain(path: Path, agent_id: str = "renee_persona") -> dict:
    att = {
        "agent_id": agent_id,
        "action": "session_genesis",
        "timestamp": "2026-04-21T19:30:00+00:00",
        "state_hash": "a" * 64,
        "prev_hash": "0" * 64,
        "signature": "deadbeef" * 8,
        "metadata": {},
    }
    path.write_text(json.dumps(att, sort_keys=True) + "\n", encoding="utf-8")
    return att


def _make_session(
    sessions_root: Path,
    session_id: str,
    *,
    public: bool = True,
    presence_score=4,
    github_published: bool = False,
    notes: str | None = None,
    flags: list | None = None,
    include_wavs: bool = True,
    chain: bool = True,
    manifest_extras: dict | None = None,
) -> Path:
    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True)
    if include_wavs:
        _write_wav(session_dir / "mic.wav")
        _write_wav(session_dir / "renee.wav")
    manifest = {
        "session_id": session_id,
        "start_time": "2026-04-21T19:30:00+00:00",
        "end_time": "2026-04-21T19:45:00+00:00",
        "renee_versions": {"persona": "0.5.5"},
        "backend_used": "cascade",
        "pod_id": None,
        "starter_metadata": {},
        "public": public,
        "reviewed": False,
        "github_published": github_published,
        "presence_score": presence_score,
        "notes_file": str(session_dir / "notes.md"),
        "genesis_session": True,
        "memory_snapshot": {},
    }
    if manifest_extras:
        manifest.update(manifest_extras)
    (session_dir / "session_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    (session_dir / "flags.json").write_text(
        json.dumps(flags or []), encoding="utf-8",
    )
    (session_dir / "eval_scores.json").write_text("[]", encoding="utf-8")
    (session_dir / "transcript.json").write_text("[]", encoding="utf-8")
    (session_dir / "latency.json").write_text(
        json.dumps({"count": 0, "p50_s": 0, "p95_s": 0, "p99_s": 0}),
        encoding="utf-8",
    )
    (session_dir / "renee_prosody.json").write_text(
        json.dumps({"windows": []}), encoding="utf-8",
    )
    (session_dir / "overlap_events.json").write_text(
        json.dumps({"events": []}), encoding="utf-8",
    )
    if chain:
        _write_chain(session_dir / "attestation_chain.jsonl")
    (session_dir / "notes.md").write_text(
        notes or "# notes\ncontent here\n", encoding="utf-8",
    )
    return session_dir


# ---------------------------------------------------------------------------
# hard gates
# ---------------------------------------------------------------------------


def test_cannot_publish_private_session(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1", public=False, presence_score=3)
    with pytest.raises(PublishError) as ei:
        stage_session(sessions_root, "s1")
    assert "not public" in str(ei.value).lower()


def test_cannot_publish_without_presence_score(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1", public=True, presence_score=None)
    with pytest.raises(PublishError) as ei:
        stage_session(sessions_root, "s1")
    assert "presence_score" in str(ei.value)


def test_cannot_publish_unknown_session(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    with pytest.raises(PublishError):
        stage_session(sessions_root, "ghost")


# ---------------------------------------------------------------------------
# staging contents + redaction
# ---------------------------------------------------------------------------


def test_stage_session_writes_only_non_wav_files_by_default(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    result = stage_session(sessions_root, "s1")
    staging = Path(result["staging_dir"])
    assert staging.exists()
    files = {p.name for p in staging.iterdir()}
    assert "session_manifest.json" in files
    assert "notes.md" in files
    assert "attestation_chain.jsonl" in files
    assert "chain_manifest.json" in files
    assert "mic.wav" not in files
    assert "renee.wav" not in files
    assert result["audio_included"] is False


def test_stage_session_includes_opus_only_when_opted_in(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    encoder = _EncodeRecorder()
    result = stage_session(sessions_root, "s1", include_audio=True, encode_fn=encoder)
    staging = Path(result["staging_dir"])
    files = {p.name for p in staging.iterdir()}
    assert "mic.opus" in files
    assert "renee.opus" in files
    assert "mic.wav" not in files
    assert "renee.wav" not in files
    assert len(encoder.calls) == 2
    assert result["audio_included"] is True


def test_redaction_creates_placeholder_rules_on_first_run(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    stage_session(sessions_root, "s1")
    rules_path = sessions_root / "redaction_rules.json"
    assert rules_path.exists()
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    assert any("PLACEHOLDER" in r["pattern"] for r in data["rules"])


def test_redaction_rules_applied_to_notes(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    (sessions_root / "redaction_rules.json").write_text(
        json.dumps(
            {"rules": [{"pattern": "Alice Smith", "replacement": "[STUDENT]"}]},
        ),
        encoding="utf-8",
    )
    _make_session(
        sessions_root, "s1",
        notes="# notes\nSession with Alice Smith went well.\n",
    )
    result = stage_session(sessions_root, "s1")
    staged_notes = (Path(result["staging_dir"]) / "notes.md").read_text(encoding="utf-8")
    assert "Alice Smith" not in staged_notes
    assert "[STUDENT]" in staged_notes


def test_chain_manifest_present_in_staging(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    result = stage_session(sessions_root, "s1")
    chain_manifest = json.loads(
        (Path(result["staging_dir"]) / "chain_manifest.json").read_text(encoding="utf-8"),
    )
    assert chain_manifest["session_id"] == "s1"
    assert chain_manifest["attestation"]["prev_hash"] == "0" * 64
    assert "continuity_note" in chain_manifest


# ---------------------------------------------------------------------------
# confirm gate
# ---------------------------------------------------------------------------


def test_publish_without_confirm_only_stages(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    git = _GitRecorder()
    result = publish_session(
        sessions_root, "s1",
        confirm=False, git_run_fn=git,
        config=PublishConfig(target_repo_local=str(tmp_path / "target")),
    )
    assert result["published"] is False
    assert result["staged"] is True
    assert git.calls == []
    manifest = json.loads(
        (sessions_root / "s1" / "session_manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest["github_published"] is False


def test_publish_with_confirm_calls_git_and_marks_published(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    git = _GitRecorder()
    cfg = PublishConfig(target_repo_local=str(tmp_path / "target"))
    result = publish_session(
        sessions_root, "s1",
        confirm=True, git_run_fn=git, config=cfg,
    )
    assert result["published"] is True
    assert any(a[0][0:2] == ["git", "add"] for a in git.calls)
    assert any(a[0][0:2] == ["git", "commit"] for a in git.calls)
    assert any(a[0][0:2] == ["git", "push"] for a in git.calls)
    manifest = json.loads(
        (sessions_root / "s1" / "session_manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest["github_published"] is True
    assert (Path(cfg.target_repo_local) / "sessions" / "s1" / "session_manifest.json").exists()


def test_publish_git_failure_raises(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    failing_git = _GitRecorder(returncode=1)
    cfg = PublishConfig(target_repo_local=str(tmp_path / "target"))
    with pytest.raises(PublishError):
        publish_session(
            sessions_root, "s1",
            confirm=True, git_run_fn=failing_git, config=cfg,
        )


# ---------------------------------------------------------------------------
# list + unpublish
# ---------------------------------------------------------------------------


def test_publish_list_filters_private_and_already_published(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "public_unpub", public=True, presence_score=4)
    _make_session(sessions_root, "private", public=False, presence_score=3)
    _make_session(sessions_root, "already_pub", public=True, presence_score=3, github_published=True)
    rows = list_publishable(sessions_root)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "public_unpub"


def test_publish_list_flags_missing_presence_score(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1", public=True, presence_score=None)
    rows = list_publishable(sessions_root)
    assert len(rows) == 1
    assert rows[0]["has_presence_score"] is False


def test_unpublish_removes_from_target_and_flips_manifest(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1", github_published=True)
    target_local = tmp_path / "target"
    (target_local / "sessions" / "s1").mkdir(parents=True)
    (target_local / "sessions" / "s1" / "notes.md").write_text(
        "stub", encoding="utf-8",
    )
    cfg = PublishConfig(target_repo_local=str(target_local))
    git = _GitRecorder()
    result = unpublish_session(sessions_root, "s1", config=cfg, git_run_fn=git)
    assert result["removed_locally"] is True
    assert not (target_local / "sessions" / "s1").exists()
    manifest = json.loads(
        (sessions_root / "s1" / "session_manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest["github_published"] is False
    assert any(a[0][0:2] == ["git", "push"] for a in git.calls)


# ---------------------------------------------------------------------------
# human-readable staging
# ---------------------------------------------------------------------------


def test_staging_is_human_readable(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    result = stage_session(sessions_root, "s1")
    staging = Path(result["staging_dir"])
    notes = (staging / "notes.md").read_text(encoding="utf-8")
    assert notes.strip().startswith("# notes")
    manifest = json.loads(
        (staging / "session_manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest["session_id"] == "s1"


def test_published_chain_manifest_references_local_chain(tmp_path):
    sessions_root = tmp_path / "sessions"
    _make_session(sessions_root, "s1")
    result = stage_session(sessions_root, "s1")
    local_chain = json.loads(
        "".join(
            (sessions_root / "s1" / "attestation_chain.jsonl").read_text(encoding="utf-8").splitlines()
        ),
    )
    published_chain = json.loads(
        (Path(result["staging_dir"]) / "chain_manifest.json").read_text(encoding="utf-8"),
    )
    assert published_chain["attestation"]["signature"] == local_chain["signature"]
    assert published_chain["attestation"]["prev_hash"] == local_chain["prev_hash"]
