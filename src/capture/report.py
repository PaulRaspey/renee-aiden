"""Per-session post-session report.

Pulls everything we know about a captured session — manifest, transcript,
triage flags, presence score, cost contribution, topic — into one
Markdown document Paul can paste into a debrief, a journal entry, or a
GitHub issue.

Reads only files; writes to ``<session_dir>/report.md`` so the output is
discoverable from the dashboard's Sessions tab. Reasonable defaults so a
session captured without a topic / triage / score still produces a
sensible report (just with the unknown fields marked).
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ReportInputs:
    """Best-effort union of every signal we have about one session.

    Each field is optional; the renderer treats absent data as "no
    evidence" rather than failure."""
    session_id: str
    session_dir: Path
    manifest: Optional[dict] = None
    transcript: Optional[list[dict]] = None
    triage: Optional[dict] = None
    notes: Optional[str] = None
    highlights_md: Optional[str] = None


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_text(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def gather(session_dir: Path) -> ReportInputs:
    """Read every artifact in a session directory we know about. Missing
    files turn into None — the renderer handles them.
    """
    sd = Path(session_dir)
    manifest = _load_json(sd / "session_manifest.json")
    transcript_raw = _load_json(sd / "transcript.json")
    transcript = transcript_raw if isinstance(transcript_raw, list) else None
    triage = _load_json(sd / "triage_results.json") or _load_json(sd / "triage.json")
    notes = _load_text(sd / "notes.md")
    highlights = _load_text(sd / "HIGHLIGHTS.md") or _load_text(sd / "highlights.md")
    return ReportInputs(
        session_id=sd.name,
        session_dir=sd,
        manifest=manifest,
        transcript=transcript,
        triage=triage,
        notes=notes,
        highlights_md=highlights,
    )


def render(inputs: ReportInputs) -> str:
    """Render the session report as Markdown."""
    out: list[str] = []
    out.append(f"# Session report — {inputs.session_id}")
    out.append("")
    m = inputs.manifest or {}
    if m:
        out.append("## Overview")
        if m.get("start_time"):
            out.append(f"- Started: {m['start_time']}")
        if m.get("end_time"):
            out.append(f"- Ended: {m['end_time']}")
        if m.get("backend_used"):
            out.append(f"- Backend: {m['backend_used']}")
        if m.get("pod_id"):
            out.append(f"- Pod: {m['pod_id']}")
        if m.get("starter_metadata"):
            sm = m["starter_metadata"]
            if isinstance(sm, dict) and sm.get("topic"):
                out.append(f"- Topic: {sm['topic']}")
        score = m.get("presence_score")
        if score is not None:
            out.append(f"- Presence score: {score}/5")
        if m.get("public") is not None:
            out.append(f"- Public: {m['public']}")
        if m.get("github_published") is not None:
            out.append(f"- Published: {m['github_published']}")
        out.append("")

    # Triage summary
    if inputs.triage:
        flags = inputs.triage.get("flags") or []
        out.append("## Triage")
        out.append(f"- Flag count: {len(flags)}")
        cats: dict[str, int] = {}
        for f in flags:
            c = (f.get("category") or "uncategorized").lower()
            cats[c] = cats.get(c, 0) + 1
        for cat in sorted(cats):
            out.append(f"  - {cat}: {cats[cat]}")
        if "fatigue_score" in inputs.triage:
            out.append(f"- Fatigue score: {inputs.triage['fatigue_score']}")
        if "presence_score" in inputs.triage and inputs.triage["presence_score"] is not None:
            out.append(f"- Presence score (from triage): {inputs.triage['presence_score']}/5")
        # Highest-severity flags inline so the reader sees what mattered
        high = [f for f in flags if (f.get("severity") or "").lower() == "high"]
        if high:
            out.append("")
            out.append("### High-severity flags")
            for f in high[:10]:
                ts = f.get("timestamp", "?")
                cat = f.get("category", "?")
                msg = f.get("message", f.get("description", ""))
                out.append(f"- [{ts}] **{cat}**: {msg}")
        out.append("")
    else:
        out.append("## Triage")
        out.append("- (no triage results — run `python -m renee triage <session>`)")
        out.append("")

    # Transcript summary
    if inputs.transcript:
        n_user = sum(1 for e in inputs.transcript if (e.get("speaker") or "").lower() == "paul")
        n_renee = sum(1 for e in inputs.transcript
                      if (e.get("speaker") or "").lower() in ("renee", "aiden", "matt"))
        out.append("## Transcript")
        out.append(f"- {len(inputs.transcript)} events ({n_user} user, {n_renee} assistant)")
        if inputs.transcript:
            first = inputs.transcript[0]
            last = inputs.transcript[-1]
            out.append(f"- First: [{first.get('speaker', '?')}] {(first.get('text') or '')[:80]}…")
            out.append(f"- Last:  [{last.get('speaker', '?')}] {(last.get('text') or '')[:80]}…")
        out.append("")

    # Highlights & notes (raw inclusion is fine — they're already MD)
    if inputs.highlights_md:
        out.append("## Highlights")
        out.append(inputs.highlights_md.strip())
        out.append("")
    if inputs.notes:
        out.append("## Notes")
        out.append(inputs.notes.strip())
        out.append("")

    out.append("---")
    out.append(f"_Generated {_dt.datetime.now().isoformat(timespec='seconds')}_")
    return "\n".join(out) + "\n"


def write_report(session_dir: Path, *, filename: str = "report.md") -> Path:
    """Convenience wrapper: gather + render + persist + return path."""
    inputs = gather(session_dir)
    md = render(inputs)
    out = Path(session_dir) / filename
    out.write_text(md, encoding="utf-8")
    return out


__all__ = ["ReportInputs", "gather", "render", "write_report"]
