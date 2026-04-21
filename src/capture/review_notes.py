"""Review notes surface.

One notes.md per session, created on first review with a template
derived from manifest + flags.json. Tags in the notes
(#harvest, #fix, #moment) aggregate into a cross-session HIGHLIGHTS.md
under the sessions root. Public sessions land in HIGHLIGHTS.md; every
session (public or not) also lands in HIGHLIGHTS_PRIVATE.md so PJ
never has to dig through individual session files.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


HARVEST_TAG = "harvest"
FIX_TAG = "fix"
MOMENT_TAG = "moment"
DEFAULT_TAGS: tuple[str, ...] = (HARVEST_TAG, FIX_TAG, MOMENT_TAG)


_TAG_PATTERN = re.compile(r"(?<![A-Za-z0-9_])#([a-zA-Z][a-zA-Z0-9_-]*)")


def _format_timestamp(seconds: Optional[float]) -> str:
    if seconds is None:
        return "session-level"
    total = int(max(0.0, float(seconds)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_duration(manifest: dict) -> str:
    start = manifest.get("start_time", "")
    end = manifest.get("end_time", "") or start
    try:
        start_dt = _dt.datetime.fromisoformat(start)
        end_dt = _dt.datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return "?"
    total_s = int(max(0.0, (end_dt - start_dt).total_seconds()))
    m = total_s // 60
    s = total_s % 60
    return f"{m:02d}:{s:02d}"


def initial_notes_content(manifest: dict, flags: list[dict]) -> str:
    sid = manifest.get("session_id", "")
    duration = _format_duration(manifest)
    backend = manifest.get("backend_used", "unknown")
    starter = manifest.get("starter_metadata") or {}
    starter_idx = starter.get("starter_index", "?")
    starter_line = f"#{starter_idx}"
    planned = starter.get("curveball_planned_minute")
    actual = starter.get("curveball_actual_minute")
    curveball_line = (
        f"planned {planned:02d}:00, actual {actual:02d}:00"
        if isinstance(planned, int) and isinstance(actual, int)
        else "n/a"
    )
    header = (
        f"# Session {sid}\n\n"
        "## Overview\n"
        f"- Duration: {duration}\n"
        f"- Backend: {backend}\n"
        f"- Starter: {starter_line}\n"
        f"- Curveball at: {curveball_line}\n"
        "- Presence score: (rate 1-5)\n\n"
        "## Flags\n"
    )
    if not flags:
        return header + "\n(no flags surfaced by triage)\n"
    blocks: list[str] = []
    for f in flags:
        ts = _format_timestamp(f.get("timestamp"))
        category = f.get("category", "?")
        severity = f.get("severity", "?")
        description = (f.get("description") or "").strip()
        blocks.append(
            f"\n### [{ts}] {category}, {severity}\n"
            f"{description}\n\n"
            "PJ notes:\n"
        )
    return header + "".join(blocks)


def ensure_notes_exists(
    session_dir: Path,
    *,
    manifest: dict | None = None,
    flags: list[dict] | None = None,
) -> Path:
    notes_path = session_dir / "notes.md"
    if notes_path.exists():
        return notes_path
    if manifest is None:
        manifest_path = session_dir / "session_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {}
    if flags is None:
        flags_path = session_dir / "flags.json"
        if flags_path.exists():
            try:
                flags = json.loads(flags_path.read_text(encoding="utf-8")) or []
            except json.JSONDecodeError:
                flags = []
        else:
            flags = []
    notes_path.write_text(initial_notes_content(manifest, flags), encoding="utf-8")
    return notes_path


def read_notes(session_dir: Path) -> str:
    notes_path = session_dir / "notes.md"
    if not notes_path.exists():
        return ""
    return notes_path.read_text(encoding="utf-8")


def save_notes(session_dir: Path, content: str) -> Path:
    notes_path = session_dir / "notes.md"
    notes_path.write_text(content, encoding="utf-8")
    return notes_path


def find_tags(text: str) -> list[str]:
    return sorted(set(_TAG_PATTERN.findall(text or "")))


@dataclass
class NotesBlock:
    level: int
    heading: str
    body: str

    @property
    def tags(self) -> list[str]:
        return find_tags(f"{self.heading}\n{self.body}")


def parse_blocks(notes_content: str) -> list[NotesBlock]:
    """Split markdown notes into heading-delimited blocks. Recognizes
    level 2 and level 3 headings. The body of a block is every line
    between its heading and the next heading or EOF."""
    blocks: list[NotesBlock] = []
    current: Optional[NotesBlock] = None
    for raw in (notes_content or "").splitlines():
        if raw.startswith("### "):
            if current is not None:
                blocks.append(current)
            current = NotesBlock(level=3, heading=raw[4:].strip(), body="")
        elif raw.startswith("## "):
            if current is not None:
                blocks.append(current)
            current = NotesBlock(level=2, heading=raw[3:].strip(), body="")
        elif current is not None:
            current.body += ("" if not current.body else "\n") + raw
    if current is not None:
        blocks.append(current)
    for b in blocks:
        b.body = b.body.strip("\n")
    return blocks


@dataclass
class TaggedBlock:
    session_id: str
    start_time: str
    public: bool
    presence_score: Optional[int]
    heading: str
    body: str
    tags: list[str]


def _iter_session_dirs(sessions_root: Path):
    if not sessions_root.exists():
        return
    for p in sorted(sessions_root.iterdir()):
        if not p.is_dir():
            continue
        if not (p / "session_manifest.json").exists():
            continue
        yield p


def collect_tagged_blocks(sessions_root: Path) -> list[TaggedBlock]:
    out: list[TaggedBlock] = []
    for session_dir in _iter_session_dirs(sessions_root):
        notes = read_notes(session_dir)
        if not notes.strip():
            continue
        try:
            manifest = json.loads(
                (session_dir / "session_manifest.json").read_text(encoding="utf-8")
            )
        except json.JSONDecodeError:
            manifest = {}
        sid = manifest.get("session_id", session_dir.name)
        start_time = manifest.get("start_time", "")
        public = bool(manifest.get("public", False))
        presence = manifest.get("presence_score")
        for b in parse_blocks(notes):
            tags = [t for t in b.tags if t in DEFAULT_TAGS]
            if not tags:
                continue
            out.append(
                TaggedBlock(
                    session_id=sid,
                    start_time=start_time,
                    public=public,
                    presence_score=presence if isinstance(presence, int) else None,
                    heading=b.heading,
                    body=b.body,
                    tags=tags,
                )
            )
    return out


def _render_highlights(
    blocks: list[TaggedBlock], *, label: str, generated_iso: str,
) -> str:
    lines = [f"# {label}", "", f"Generated: {generated_iso}", ""]
    by_tag: dict[str, list[TaggedBlock]] = {t: [] for t in DEFAULT_TAGS}
    for b in blocks:
        for t in b.tags:
            if t in by_tag:
                by_tag[t].append(b)
    for tag in DEFAULT_TAGS:
        hits = sorted(
            by_tag[tag],
            key=lambda b: (b.start_time or "", b.heading or ""),
        )
        lines.append(f"## #{tag}")
        lines.append("")
        if not hits:
            lines.append("_no entries yet_")
            lines.append("")
            continue
        for b in hits:
            lines.append(f"### {b.session_id} - {b.heading}")
            lines.append("")
            if b.body:
                lines.append(b.body)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def regenerate_highlights(
    sessions_root: Path,
    *,
    now: Optional[_dt.datetime] = None,
) -> dict:
    """Rebuild HIGHLIGHTS.md (public only) and HIGHLIGHTS_PRIVATE.md (all
    tagged blocks) in sessions_root. Returns counts for PJ's CLI output.
    Safe to run on an empty or nonexistent sessions_root; writes empty
    shells in that case."""
    sessions_root = Path(sessions_root)
    sessions_root.mkdir(parents=True, exist_ok=True)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    generated_iso = now.isoformat()

    all_blocks = collect_tagged_blocks(sessions_root)
    public_blocks = [b for b in all_blocks if b.public]

    (sessions_root / "HIGHLIGHTS.md").write_text(
        _render_highlights(
            public_blocks,
            label="Renee session highlights (public)",
            generated_iso=generated_iso,
        ),
        encoding="utf-8",
    )
    (sessions_root / "HIGHLIGHTS_PRIVATE.md").write_text(
        _render_highlights(
            all_blocks,
            label="Renee session highlights (private)",
            generated_iso=generated_iso,
        ),
        encoding="utf-8",
    )
    return {
        "public_block_count": len(public_blocks),
        "private_block_count": len(all_blocks),
        "public_path": str(sessions_root / "HIGHLIGHTS.md"),
        "private_path": str(sessions_root / "HIGHLIGHTS_PRIVATE.md"),
    }
