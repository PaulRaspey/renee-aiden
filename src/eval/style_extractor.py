"""
Style-reference extractor (M11 / M12 companion).

Parses `scripts/renee_reference_script.md` (original work, NOT derived from
any copyrighted screenplay — see the file header) and emits statistical
patterns to `configs/style_reference.yaml`. The extracted patterns feed
the persona prompt and the prosody density tuning.

What we extract from the script:
  - turn_length: median, mean, p95, min, max — both personas and Renée-only
  - hedge_frequency: rate of hedge markers across Renée factual turns
  - paralinguistic_events_per_turn (Renée): overall and by scene
  - pause_distribution: counts of (beat), (long beat), trailing silence
  - false_start_rate: fraction of Renée turns with a false-start marker
  - silent_response_count: Renée turns that are *only* silence
  - voice_register_shifts: count of (quiet) / (warmth) / (dry) markers
  - dry_humor_markers
  - warmth_markers
  - vulnerability_markers
  - callback_inventory: textual anchor references across scenes
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable

import yaml


# Notation patterns from the reference script's key.
_BEAT_PAT = re.compile(r"\(beat\)")
_LONG_BEAT_PAT = re.compile(r"\(long beat\)")
_TRAILING_PAT = re.compile(r"(?<!\S)\.\.\.(?!\S)")   # "..." standalone
_TRAIL_OFF_PAT = re.compile(r"\(trailing off\)")
_FALSE_START_PAT = re.compile(r"\(false start\)")
_BREATH_IN_PAT = re.compile(r"\(breath in\)")
_BREATH_OUT_PAT = re.compile(r"\(breath out\)")
_SIGH_PAT = re.compile(r"\(sigh\)")
_SOFT_LAUGH_PAT = re.compile(r"\(soft laugh\)")
_LAUGH_PAT = re.compile(r"\(laugh\)")
_SUPPRESSED_LAUGH_PAT = re.compile(r"\(suppressed laugh\)")
_THINKING_PAT = re.compile(r"\(thinking\)")
_QUIET_PAT = re.compile(r"\(quiet\)")
_WARMTH_PAT = re.compile(r"\(warmth\)")
_DRY_PAT = re.compile(r"\(dry\)")
_SHARP_PAT = re.compile(r"\(sharp\)")
_VULN_PAT = re.compile(r"\(vulnerable\)")

# Hedge markers the eval scorer also uses. Keep in rough sync.
_HEDGE_MARKERS = (
    "i think", "i'd guess", "i'd say", "maybe", "probably",
    "sort of", "kind of", "i'm not sure", "might", "could be",
    "seems", "feels like", "i suppose", "i don't know",
    "pretty sure", "not totally",
)

# Speaker line prefix. The script uses "PAUL:" and "RENÉE:" (with accent).
_SPEAKER_RE = re.compile(r"^([A-ZÀ-Ÿ]{2,}):\s*(.*)$")


@dataclass
class TurnRecord:
    speaker: str
    text: str
    markers: list[str]
    word_count: int
    silent: bool


def _strip_markers(text: str) -> str:
    """Remove (parenthesized) markers and scene annotations, return prose only."""
    cleaned = re.sub(r"\([^)]*\)", "", text)
    cleaned = re.sub(r"\.\.\.", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _collect_markers(text: str) -> list[str]:
    markers = []
    for pat, name in [
        (_BEAT_PAT, "beat"),
        (_LONG_BEAT_PAT, "long_beat"),
        (_TRAIL_OFF_PAT, "trailing_off"),
        (_FALSE_START_PAT, "false_start"),
        (_BREATH_IN_PAT, "breath_in"),
        (_BREATH_OUT_PAT, "breath_out"),
        (_SIGH_PAT, "sigh"),
        (_SOFT_LAUGH_PAT, "soft_laugh"),
        (_SUPPRESSED_LAUGH_PAT, "suppressed_laugh"),
        (_LAUGH_PAT, "laugh"),
        (_THINKING_PAT, "thinking"),
        (_QUIET_PAT, "quiet"),
        (_WARMTH_PAT, "warmth"),
        (_DRY_PAT, "dry"),
        (_SHARP_PAT, "sharp"),
        (_VULN_PAT, "vulnerable"),
    ]:
        for _ in pat.finditer(text):
            markers.append(name)
    return markers


def parse_script(script_text: str) -> list[TurnRecord]:
    """Walk lines, accumulate speaker turns."""
    turns: list[TurnRecord] = []
    current_speaker: str | None = None
    current_buf: list[str] = []

    def _flush():
        if current_speaker is None:
            return
        raw = "\n".join(current_buf).strip()
        if not raw:
            return
        markers = _collect_markers(raw)
        prose = _strip_markers(raw)
        word_count = len(prose.split()) if prose else 0
        silent = word_count == 0 and bool(markers)
        turns.append(TurnRecord(
            speaker=current_speaker,
            text=prose,
            markers=markers,
            word_count=word_count,
            silent=silent,
        ))

    for line in script_text.splitlines():
        if line.strip().startswith("#"):
            continue
        m = _SPEAKER_RE.match(line)
        if m:
            _flush()
            current_speaker = m.group(1)
            current_buf = [m.group(2)]
        elif current_speaker is not None:
            current_buf.append(line)
    _flush()
    return turns


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * len(s)) - 1)))
    return s[idx]


def _hedge_rate_across(turns: Iterable[TurnRecord]) -> float:
    factual = 0
    hedged = 0
    for t in turns:
        if not t.text or t.text.endswith("?"):
            continue
        factual += 1
        low = t.text.lower()
        if any(m in low for m in _HEDGE_MARKERS):
            hedged += 1
    if factual == 0:
        return 0.0
    return round(hedged / factual, 3)


def aggregate(turns: list[TurnRecord]) -> dict:
    renee_turns = [t for t in turns if t.speaker.startswith("REN")]
    paul_turns = [t for t in turns if t.speaker.startswith("PAUL")]

    def _word_stats(ts: list[TurnRecord]) -> dict:
        words = [t.word_count for t in ts if t.word_count > 0]
        if not words:
            return {"count": 0}
        return {
            "count": len(ts),
            "words_mean": round(mean(words), 2),
            "words_median": int(median(words)),
            "words_p95": int(_percentile([float(x) for x in words], 95)),
            "words_min": min(words),
            "words_max": max(words),
        }

    def _marker_counts(ts: list[TurnRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in ts:
            for m in t.markers:
                counts[m] = counts.get(m, 0) + 1
        return counts

    renee_markers = _marker_counts(renee_turns)
    renee_paralinguistic_keys = {
        "breath_in", "breath_out", "sigh", "soft_laugh", "laugh",
        "suppressed_laugh", "thinking",
    }
    paralinguistic_total = sum(v for k, v in renee_markers.items() if k in renee_paralinguistic_keys)
    paralinguistic_per_turn = (
        round(paralinguistic_total / max(1, len(renee_turns)), 3)
    )

    false_starts = renee_markers.get("false_start", 0)
    silent_count = sum(1 for t in renee_turns if t.silent)

    pause_markers = {
        "beat": renee_markers.get("beat", 0),
        "long_beat": renee_markers.get("long_beat", 0),
        "trailing_off": renee_markers.get("trailing_off", 0),
    }
    register_markers = {
        "quiet": renee_markers.get("quiet", 0),
        "warmth": renee_markers.get("warmth", 0),
        "dry": renee_markers.get("dry", 0),
        "sharp": renee_markers.get("sharp", 0),
        "vulnerable": renee_markers.get("vulnerable", 0),
    }

    hedge_rate = _hedge_rate_across(renee_turns)

    return {
        "source": "scripts/renee_reference_script.md",
        "extracted_for": "renee",
        "totals": {
            "turns_total": len(turns),
            "renee_turns": len(renee_turns),
            "paul_turns": len(paul_turns),
        },
        "turn_length": {
            "renee": _word_stats(renee_turns),
            "paul": _word_stats(paul_turns),
        },
        "hedge_frequency_renee": hedge_rate,
        "paralinguistics_per_turn_renee": paralinguistic_per_turn,
        "marker_counts_renee": renee_markers,
        "pause_markers_renee": pause_markers,
        "register_markers_renee": register_markers,
        "false_start_count_renee": false_starts,
        "false_start_rate_renee": round(false_starts / max(1, len(renee_turns)), 3),
        "silent_response_count_renee": silent_count,
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def extract(script_path: Path) -> dict:
    script = script_path.read_text(encoding="utf-8")
    turns = parse_script(script)
    return aggregate(turns)


def write_style_reference(data: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# configs/style_reference.yaml\n"
        "# Auto-generated by src/eval/style_extractor.py from\n"
        "# scripts/renee_reference_script.md.\n"
        "# DO NOT EDIT BY HAND. Regenerate with:\n"
        "#   python -m src.eval.style_extractor\n\n"
    )
    out_path.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "renee_reference_script.md"
    out_path = repo_root / "configs" / "style_reference.yaml"
    data = extract(script_path)
    write_style_reference(data, out_path)
    print(f"wrote {out_path} ({data['totals']['turns_total']} turns parsed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
