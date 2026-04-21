"""Read helpers for the dashboard Sessions tab.

Kept separate from the FastAPI surface so the aggregation logic is unit
testable without spinning up TestClient and so the dashboard module does
not grow a second time.
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path
from statistics import mean
from typing import Optional


DASHBOARD_AUDIO_NAMES = {"mic.wav", "renee.wav"}


def _parse_iso(s: str) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _iter_session_dirs(sessions_root: Path):
    if not sessions_root.exists():
        return
    for p in sorted(sessions_root.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        if not (p / "session_manifest.json").exists():
            continue
        yield p


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def list_sessions(sessions_root: Path) -> list[dict]:
    out: list[dict] = []
    for p in _iter_session_dirs(sessions_root):
        manifest = _load_json(p / "session_manifest.json", {})
        flags = _load_json(p / "flags.json", [])
        start = _parse_iso(manifest.get("start_time", ""))
        end = _parse_iso(manifest.get("end_time", ""))
        duration_s = (end - start).total_seconds() if start and end else 0.0
        out.append(
            {
                "session_id": manifest.get("session_id"),
                "start_time": manifest.get("start_time"),
                "end_time": manifest.get("end_time"),
                "duration_s": duration_s,
                "backend_used": manifest.get("backend_used"),
                "flag_count": len(flags) if isinstance(flags, list) else 0,
                "presence_score": manifest.get("presence_score"),
                "public": manifest.get("public", False),
                "github_published": manifest.get("github_published", False),
                "reviewed": manifest.get("reviewed", False),
                "genesis_session": manifest.get("genesis_session", False),
            }
        )
    return out


def session_detail(sessions_root: Path, session_id: str) -> dict:
    session_dir = sessions_root / session_id
    if not session_dir.exists() or not (session_dir / "session_manifest.json").exists():
        raise FileNotFoundError(f"session not found: {session_id}")
    manifest = _load_json(session_dir / "session_manifest.json", {})
    flags = _load_json(session_dir / "flags.json", [])
    prosody = _load_json(session_dir / "renee_prosody.json", {"windows": []})
    latency = _load_json(session_dir / "latency.json", {"count": 0})
    overlap = _load_json(session_dir / "overlap_events.json", {"events": []})
    eval_scores = _load_json(session_dir / "eval_scores.json", [])
    notes_path = session_dir / "notes.md"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    return {
        "session_id": session_id,
        "manifest": manifest,
        "flags": flags if isinstance(flags, list) else [],
        "prosody": prosody,
        "latency": latency,
        "overlap_events": overlap,
        "eval_scores": eval_scores,
        "notes": notes,
        "mic_wav_url": f"/api/sessions/{session_id}/audio/mic.wav",
        "renee_wav_url": f"/api/sessions/{session_id}/audio/renee.wav",
    }


def resolve_session_audio(sessions_root: Path, session_id: str, name: str) -> Path:
    if name not in DASHBOARD_AUDIO_NAMES:
        raise ValueError(f"unknown audio name: {name!r}")
    session_dir = sessions_root / session_id
    resolved = (session_dir / name).resolve()
    root_resolved = sessions_root.resolve()
    if root_resolved not in resolved.parents:
        raise ValueError("audio path escapes sessions root")
    if not resolved.exists():
        raise FileNotFoundError(f"audio not found: {session_id}/{name}")
    return resolved


def session_trends(sessions_root: Path) -> dict:
    sessions: list[dict] = []
    for p in _iter_session_dirs(sessions_root):
        manifest = _load_json(p / "session_manifest.json", {})
        flags = _load_json(p / "flags.json", [])
        latency = _load_json(p / "latency.json", {})
        start = _parse_iso(manifest.get("start_time", ""))
        end = _parse_iso(manifest.get("end_time", ""))
        duration_s = (end - start).total_seconds() if start and end else 0.0
        flag_categories: dict[str, int] = {}
        safety_count = 0
        overlap_count = 0
        if isinstance(flags, list):
            for f in flags:
                cat = f.get("category", "?")
                flag_categories[cat] = flag_categories.get(cat, 0) + 1
                if cat == "safety_trigger":
                    safety_count += 1
                if cat == "overlap":
                    overlap_count += 1
        eval_rows = _load_json(p / "eval_scores.json", [])
        overall_scores = []
        if isinstance(eval_rows, list):
            for row in eval_rows:
                scores = row.get("scores") or {}
                ov = scores.get("overall") if isinstance(scores, dict) else None
                val = None
                if isinstance(ov, dict) and "value" in ov:
                    val = float(ov.get("value", 0.0))
                elif isinstance(ov, (int, float)):
                    val = float(ov)
                if val is not None:
                    overall_scores.append(val)
        mean_overall = mean(overall_scores) if overall_scores else None
        sessions.append(
            {
                "session_id": manifest.get("session_id"),
                "date": start.date().isoformat() if start else "",
                "start_time": manifest.get("start_time"),
                "duration_s": duration_s,
                "flag_total": len(flags) if isinstance(flags, list) else 0,
                "flag_categories": flag_categories,
                "safety_count": safety_count,
                "safety_rate_per_min": (
                    safety_count / (duration_s / 60.0) if duration_s > 0 else 0.0
                ),
                "overlap_count": overlap_count,
                "latency_p50_s": float(latency.get("p50_s", 0.0)) if latency else 0.0,
                "latency_p95_s": float(latency.get("p95_s", 0.0)) if latency else 0.0,
                "mean_overall_score": mean_overall,
                "presence_score": manifest.get("presence_score"),
            }
        )
    return {"sessions": sessions, "count": len(sessions)}


def _dir_size_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def disk_usage(sessions_root: Path) -> dict:
    sessions_total = _dir_size_bytes(sessions_root)
    probe_root = sessions_root if sessions_root.exists() else sessions_root.parent
    free_bytes = 0
    total_drive_bytes = 0
    used_drive_bytes = 0
    try:
        stats = shutil.disk_usage(probe_root)
        free_bytes = stats.free
        total_drive_bytes = stats.total
        used_drive_bytes = stats.used
    except OSError:
        pass
    session_count = sum(1 for _ in _iter_session_dirs(sessions_root))
    avg_session_bytes = (
        int(sessions_total / session_count)
        if session_count
        else 100 * 1024 * 1024
    )
    days_of_runway = (
        int(free_bytes // avg_session_bytes) if avg_session_bytes > 0 else 0
    )
    soft_warn = (
        used_drive_bytes / total_drive_bytes > 0.8
        if total_drive_bytes > 0
        else False
    )
    return {
        "sessions_total_bytes": sessions_total,
        "free_bytes": free_bytes,
        "total_drive_bytes": total_drive_bytes,
        "session_count": session_count,
        "avg_session_bytes": avg_session_bytes,
        "days_of_runway": days_of_runway,
        "soft_warn_at_80pct": soft_warn,
    }


class PresenceScoreLockedError(PermissionError):
    """Raised when PJ tries to update presence_score after publish."""


def set_presence_score(
    sessions_root: Path, session_id: str, score: int,
) -> dict:
    session_dir = sessions_root / session_id
    manifest_path = session_dir / "session_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"session not found: {session_id}")
    if not isinstance(score, int) or isinstance(score, bool):
        raise ValueError("presence_score must be an integer in [1, 5]")
    if not (1 <= score <= 5):
        raise ValueError("presence_score must be an integer in [1, 5]")
    manifest = _load_json(manifest_path, {})
    if manifest.get("github_published"):
        raise PresenceScoreLockedError(
            "presence_score is locked after the session has been published"
        )
    manifest["presence_score"] = int(score)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8",
    )
    return manifest
