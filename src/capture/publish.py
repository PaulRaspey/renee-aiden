"""Selective GitHub publishing.

Three CLI commands:
  publish <session_id>       package -> staging -> optional push
  publish-list               show sessions marked public but not published
  unpublish <session_id>     remove from the target repo

Hard gates:
  - manifest.public must be True (PJ flips manually)
  - manifest.presence_score must be set (locked after publish)
  - redaction_rules.json exists in sessions_root (auto-created as a
    placeholder on first attempt; PJ fills in real patterns before the
    first real publish)

Audio excluded by default. --include-audio ships a 48 kbps mono Opus
derivative via ffmpeg; WAV masters never leave the OptiPlex. --confirm
is required before any git push; without it publish() only writes the
staging directory for human review.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import yaml


logger = logging.getLogger("renee.capture.publish")


STAGING_DIR_NAME = "_publish_staging"
DEFAULT_PUBLISH_CONFIG_PATH = Path("configs/publish.yaml")
REDACTION_RULES_NAME = "redaction_rules.json"
DEFAULT_REDACTION_RULES = {
    "rules": [
        {"pattern": "PLACEHOLDER_STUDENT_NAME", "replacement": "[STUDENT]"},
        {"pattern": "PLACEHOLDER_CLIENT_NAME", "replacement": "[CLIENT]"},
        {"pattern": "PLACEHOLDER_FAMILY_NAME", "replacement": "[FAMILY]"},
        {"pattern": "PLACEHOLDER_STREET_NUMBER \\d+", "replacement": "[ADDRESS]"},
    ],
    "note": (
        "Fill in real regex patterns PJ wants redacted before the first "
        "real publish. These placeholder patterns exist only so the first "
        "staging directory is demonstrably redactable."
    ),
}


GitRunFn = Callable[[list[str], Path], Any]
EncodeFn = Callable[[Path, Path], None]


@dataclasses.dataclass
class PublishConfig:
    target_repo_url: str = ""
    target_repo_local: str = "state/renee-sessions-public"
    target_branch: str = "main"
    commit_message_template: str = "publish session {session_id}"

    @classmethod
    def load(cls, path: Path | str) -> "PublishConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(
            target_repo_url=str(raw.get("target_repo_url") or ""),
            target_repo_local=str(raw.get("target_repo_local") or cls.target_repo_local),
            target_branch=str(raw.get("target_branch") or "main"),
            commit_message_template=str(
                raw.get("commit_message_template") or cls.commit_message_template
            ),
        )


class PublishError(RuntimeError):
    """Raised on publish-gate violation or missing dependency."""


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def load_or_create_redaction_rules(sessions_root: Path) -> dict:
    sessions_root = Path(sessions_root)
    path = sessions_root / REDACTION_RULES_NAME
    if not path.exists():
        sessions_root.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(DEFAULT_REDACTION_RULES, indent=2),
            encoding="utf-8",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PublishError(f"invalid {REDACTION_RULES_NAME}: {e}")


def _apply_rules(text: str, rules: list[dict]) -> str:
    for rule in rules:
        pattern = rule.get("pattern")
        repl = rule.get("replacement", "")
        if not pattern:
            continue
        text = re.sub(pattern, repl, text)
    return text


def _redact_text_files(root: Path, rules: list[dict]) -> None:
    text_suffixes = {".json", ".jsonl", ".md", ".txt", ".yaml", ".yml"}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in text_suffixes:
            try:
                content = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            new_content = _apply_rules(content, rules)
            if new_content != content:
                p.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# gate + listing
# ---------------------------------------------------------------------------


def list_publishable(sessions_root: Path) -> list[dict]:
    sessions_root = Path(sessions_root)
    out: list[dict] = []
    if not sessions_root.exists():
        return out
    for p in sorted(sessions_root.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        manifest_path = p / "session_manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not manifest.get("public"):
            continue
        if manifest.get("github_published"):
            continue
        out.append(
            {
                "session_id": manifest.get("session_id"),
                "presence_score": manifest.get("presence_score"),
                "has_presence_score": manifest.get("presence_score") is not None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


def _default_encode_to_opus(wav_path: Path, opus_path: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise PublishError("ffmpeg not on PATH; cannot encode Opus")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-c:a", "libopus", "-b:a", "48k", "-ac", "1",
            str(opus_path),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise PublishError(
            "ffmpeg failed encoding Opus: "
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


_STAGE_COPY_NAMES = (
    "session_manifest.json",
    "flags.json",
    "notes.md",
    "eval_scores.json",
    "renee_prosody.json",
    "overlap_events.json",
    "latency.json",
    "mic_transcript.json",
    "renee_transcript.json",
    "transcript.json",
    "attestation_chain.jsonl",
)


def _load_attestation(session_dir: Path) -> Optional[dict]:
    chain_path = session_dir / "attestation_chain.jsonl"
    if not chain_path.exists():
        return None
    for line in chain_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None
    return None


def stage_session(
    sessions_root: Path,
    session_id: str,
    *,
    include_audio: bool = False,
    encode_fn: Optional[EncodeFn] = None,
) -> dict:
    sessions_root = Path(sessions_root)
    session_dir = sessions_root / session_id
    manifest_path = session_dir / "session_manifest.json"
    if not manifest_path.exists():
        raise PublishError(f"session not found: {session_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if not manifest.get("public"):
        raise PublishError(
            f"session {session_id} is not public; flip public=true to publish"
        )
    if manifest.get("presence_score") is None:
        raise PublishError(
            f"session {session_id} lacks presence_score; hard gate before publish"
        )

    rules_data = load_or_create_redaction_rules(sessions_root)
    rules = rules_data.get("rules") or []

    staging_root = sessions_root / STAGING_DIR_NAME
    staging_dir = staging_root / session_id
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    for name in _STAGE_COPY_NAMES:
        src = session_dir / name
        if src.exists():
            shutil.copyfile(src, staging_dir / name)

    audio_included = False
    if include_audio:
        encode = encode_fn or _default_encode_to_opus
        for wav_name in ("mic.wav", "renee.wav"):
            src = session_dir / wav_name
            if src.exists():
                dst = staging_dir / wav_name.replace(".wav", ".opus")
                encode(src, dst)
                audio_included = True

    attestation = _load_attestation(session_dir)
    chain_manifest = {
        "session_id": session_id,
        "attestation": attestation,
        "continuity_note": (
            "prev_hash links this session to the previous session's "
            "attestation hash. Third parties can verify continuity by "
            "chaining public sessions in order; the chain is valid if each "
            "session's prev_hash matches the hash of the previous session's "
            "attestation."
        ),
    }
    (staging_dir / "chain_manifest.json").write_text(
        json.dumps(chain_manifest, indent=2, default=str),
        encoding="utf-8",
    )

    _redact_text_files(staging_dir, rules)

    return {
        "session_id": session_id,
        "staging_dir": str(staging_dir),
        "audio_included": audio_included,
        "redaction_rules_applied": len(rules),
        "files": sorted(p.name for p in staging_dir.iterdir()),
        "chain_manifest_present": (staging_dir / "chain_manifest.json").exists(),
    }


# ---------------------------------------------------------------------------
# git push
# ---------------------------------------------------------------------------


def _default_git(args: list[str], cwd: Path):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)


def publish_session(
    sessions_root: Path,
    session_id: str,
    *,
    include_audio: bool = False,
    confirm: bool = False,
    encode_fn: Optional[EncodeFn] = None,
    git_run_fn: Optional[GitRunFn] = None,
    config: Optional[PublishConfig] = None,
) -> dict:
    sessions_root = Path(sessions_root)
    stage_result = stage_session(
        sessions_root, session_id,
        include_audio=include_audio,
        encode_fn=encode_fn,
    )
    if not confirm:
        return {
            "ok": True,
            "staged": True,
            "published": False,
            "staging_dir": stage_result["staging_dir"],
            "audio_included": stage_result["audio_included"],
            "message": "staging written; re-run with --confirm to push",
        }

    cfg = config or PublishConfig.load(DEFAULT_PUBLISH_CONFIG_PATH)
    run = git_run_fn or _default_git
    target_local = Path(cfg.target_repo_local)
    target_local.mkdir(parents=True, exist_ok=True)
    session_target = target_local / "sessions" / session_id
    if session_target.exists():
        shutil.rmtree(session_target)
    shutil.copytree(stage_result["staging_dir"], session_target)

    git_steps: list[dict] = []
    for args in (
        ["git", "add", f"sessions/{session_id}"],
        ["git", "commit", "-m", cfg.commit_message_template.format(session_id=session_id)],
        ["git", "push", "origin", cfg.target_branch],
    ):
        r = run(args, target_local)
        rc = int(getattr(r, "returncode", 0))
        git_steps.append({"args": args, "returncode": rc})
        if rc != 0:
            raise PublishError(
                f"git command failed: {' '.join(args)} (rc={rc})"
            )

    manifest_path = sessions_root / session_id / "session_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["github_published"] = True
    manifest_path.write_text(
        json.dumps(manifest, indent=2, default=str),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "staged": True,
        "published": True,
        "staging_dir": stage_result["staging_dir"],
        "target_session_dir": str(session_target),
        "audio_included": stage_result["audio_included"],
        "git_steps": git_steps,
    }


def unpublish_session(
    sessions_root: Path,
    session_id: str,
    *,
    git_run_fn: Optional[GitRunFn] = None,
    config: Optional[PublishConfig] = None,
) -> dict:
    sessions_root = Path(sessions_root)
    cfg = config or PublishConfig.load(DEFAULT_PUBLISH_CONFIG_PATH)
    run = git_run_fn or _default_git
    target_local = Path(cfg.target_repo_local)
    session_target = target_local / "sessions" / session_id
    removed_locally = session_target.exists()
    if removed_locally:
        shutil.rmtree(session_target)
    git_steps: list[dict] = []
    for args in (
        ["git", "add", "-A", f"sessions/{session_id}"],
        ["git", "commit", "-m", f"unpublish session {session_id}"],
        ["git", "push", "origin", cfg.target_branch],
    ):
        r = run(args, target_local)
        git_steps.append({"args": args, "returncode": int(getattr(r, "returncode", 0))})

    manifest_path = sessions_root / session_id / "session_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["github_published"] = False
        manifest_path.write_text(
            json.dumps(manifest, indent=2, default=str),
            encoding="utf-8",
        )

    return {
        "ok": True,
        "session_id": session_id,
        "removed_locally": removed_locally,
        "git_steps": git_steps,
    }
