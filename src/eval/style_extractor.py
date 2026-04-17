"""
Style-reference extractor (M11 baseline, expanded in M12).

Parses `scripts/renee_reference_script.md` (original work, NOT derived from
any copyrighted screenplay - see the file header) and emits statistical
patterns to `configs/style_reference.yaml`. The extracted patterns feed
the persona prompt (via `src/persona/style_rules.py`) and the prosody
density tuning.

M11 baseline metrics:
  - turn_length: median, mean, p95, min, max
  - hedge_frequency (Renée)
  - paralinguistic_events_per_turn (Renée)
  - pause_distribution: beat, long_beat, trailing silence
  - register_markers: quiet, warmth, dry, sharp, vulnerable
  - false_start_rate
  - silent_response_count

M12 expansions:
  - turn_length percentiles: p25, p50, p75, p90, p95, p99
  - per-scene stats: scene emotional register, paralinguistic density,
    pause density, callback hits, dry-humor density
  - callback structure: anchor terms, cross-scene reappearance graph,
    explicit callback-marker hits
  - vocabulary texture: type/token ratio, top lemmas, signature phrases,
    sensory metaphor density, AI-ism check
  - emotional pacing: per-scene warmth/quiet/vulnerability/laugh density,
    mood_arc sequence (light/serious/intimate/conflict labels per scene)
  - pause distribution breakdown: beat%, long_beat%, trailing%, breath%
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
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

# Explicit callback markers in prose. Detecting anchor terms is separate.
_CALLBACK_MARKERS = (
    "you remember", "like you said", "the thing you",
    "what we talked about", "like last time", "i caught that",
    "the thing about", "from yesterday", "last night",
)

# Sensory-metaphor seed list. Renée config says she uses these more than most.
_SENSORY_WORDS = (
    "smell", "taste", "texture", "weight", "heavy", "light", "warm",
    "cold", "sharp", "soft", "rough", "smooth", "heat", "chill",
    "bitter", "sour", "sweet", "salty", "grain", "grit", "rasp",
    "quiet", "loud", "thick", "thin", "dense", "hollow",
)

# Signature phrases Renée reaches for (per the persona config).
_SIGNATURE_PHRASES = (
    "i think", "honestly though", "wait", "yeah no", "kind of",
    "sort of", "maybe", "okay but", "that's interesting", "hmm",
)

# Scene header in the script.
_SCENE_HEADER = re.compile(r"^#\s*=+\s*$\s*^#\s*SCENE\s+(\d+)[:\s-]*(.*?)$",
                           re.MULTILINE)
_SCENE_LINE_RE = re.compile(r"^#\s*SCENE\s+(\d+)[:\s-]*(.*?)$",
                            re.IGNORECASE | re.MULTILINE)

# Speaker line prefix. The script uses "PAUL:" and "RENÉE:" (with accent).
_SPEAKER_RE = re.compile(r"^([A-ZÀ-Ÿ]{2,}):\s*(.*)$")


@dataclass
class TurnRecord:
    speaker: str
    text: str
    markers: list[str]
    word_count: int
    silent: bool
    scene: int = 0  # 1-indexed; 0 = preamble / unscened


@dataclass
class SceneStats:
    scene: int
    title: str
    renee_turns: int
    paul_turns: int
    paralinguistic_count: int
    paralinguistic_per_turn: float
    warmth: int
    quiet: int
    dry: int
    sharp: int
    vulnerable: int
    laughs: int        # soft_laugh + laugh + suppressed_laugh
    sighs: int
    breaths: int
    beats: int
    long_beats: int
    callback_marker_hits: int
    mood_label: str    # light / casual / serious / intimate / conflict


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


def _scene_index_for_line(line_no: int, scene_boundaries: list[tuple[int, int, str]]) -> tuple[int, str]:
    """Return (scene_number, title) for the scene that contains `line_no`.
    Returns (0, "") before the first scene."""
    current = (0, "")
    for boundary_line, scene_num, title in scene_boundaries:
        if line_no >= boundary_line:
            current = (scene_num, title)
    return current


def _scan_scene_boundaries(text: str) -> list[tuple[int, int, str]]:
    boundaries: list[tuple[int, int, str]] = []
    for m in _SCENE_LINE_RE.finditer(text):
        line_no = text[:m.start()].count("\n")
        try:
            scene_num = int(m.group(1))
        except (TypeError, ValueError):
            continue
        title = (m.group(2) or "").strip()
        boundaries.append((line_no, scene_num, title))
    return boundaries


def parse_script(script_text: str) -> list[TurnRecord]:
    """Walk lines, accumulate speaker turns. Tag each turn with its scene index."""
    scene_boundaries = _scan_scene_boundaries(script_text)
    turns: list[TurnRecord] = []
    current_speaker: str | None = None
    current_buf: list[str] = []
    current_start_line: int = 0

    def _flush(end_line: int):
        nonlocal current_speaker, current_buf
        if current_speaker is None:
            return
        raw = "\n".join(current_buf).strip()
        if not raw:
            current_speaker = None
            current_buf = []
            return
        markers = _collect_markers(raw)
        prose = _strip_markers(raw)
        word_count = len(prose.split()) if prose else 0
        silent = word_count == 0 and bool(markers)
        scene_num, _ = _scene_index_for_line(current_start_line, scene_boundaries)
        turns.append(TurnRecord(
            speaker=current_speaker,
            text=prose,
            markers=markers,
            word_count=word_count,
            silent=silent,
            scene=scene_num,
        ))
        current_speaker = None
        current_buf = []

    for line_no, line in enumerate(script_text.splitlines()):
        if line.strip().startswith("#"):
            continue
        m = _SPEAKER_RE.match(line)
        if m:
            _flush(line_no)
            current_speaker = m.group(1)
            current_buf = [m.group(2)]
            current_start_line = line_no
        elif current_speaker is not None:
            current_buf.append(line)
    _flush(len(script_text.splitlines()))
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


def _word_stats(ts: list[TurnRecord]) -> dict:
    words = [t.word_count for t in ts if t.word_count > 0]
    if not words:
        return {"count": 0}
    floats = [float(x) for x in words]
    return {
        "count": len(ts),
        "words_mean": round(mean(words), 2),
        "words_median": int(median(words)),
        "words_p25": int(_percentile(floats, 25)),
        "words_p50": int(_percentile(floats, 50)),
        "words_p75": int(_percentile(floats, 75)),
        "words_p90": int(_percentile(floats, 90)),
        "words_p95": int(_percentile(floats, 95)),
        "words_p99": int(_percentile(floats, 99)),
        "words_min": min(words),
        "words_max": max(words),
    }


def _marker_counts(ts: list[TurnRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in ts:
        for m in t.markers:
            counts[m] = counts.get(m, 0) + 1
    return counts


_PARALINGUISTIC_KEYS = {
    "breath_in", "breath_out", "sigh", "soft_laugh", "laugh",
    "suppressed_laugh", "thinking",
}


def _paralinguistic_count(markers: dict[str, int]) -> int:
    return sum(v for k, v in markers.items() if k in _PARALINGUISTIC_KEYS)


def _callback_hits(ts: list[TurnRecord]) -> int:
    hits = 0
    for t in ts:
        low = t.text.lower()
        hits += sum(1 for m in _CALLBACK_MARKERS if m in low)
    return hits


def _scene_mood_label(s: SceneStats) -> str:
    """Rough scene-tone classification from marker density."""
    per_turn = max(1, s.renee_turns + s.paul_turns)
    if s.sharp >= 2 or (s.sighs + s.breaths) >= 5 and s.laughs == 0:
        return "conflict"
    if s.quiet >= 3 and (s.vulnerable >= 1 or s.warmth >= 3):
        return "intimate"
    if s.vulnerable >= 1 or (s.sighs >= 2 and s.quiet >= 2):
        return "serious"
    if s.laughs >= 3 or (s.dry >= 1 and s.laughs >= 1):
        return "light"
    return "casual"


def _scene_stats(turns: list[TurnRecord]) -> list[SceneStats]:
    by_scene: dict[int, list[TurnRecord]] = {}
    for t in turns:
        by_scene.setdefault(t.scene, []).append(t)

    stats: list[SceneStats] = []
    for scene_num in sorted(by_scene.keys()):
        scene_turns = by_scene[scene_num]
        if scene_num == 0 and not scene_turns:
            continue
        renee_turns = [t for t in scene_turns if t.speaker.startswith("REN")]
        paul_turns = [t for t in scene_turns if t.speaker.startswith("PAUL")]
        markers = _marker_counts(renee_turns)
        paralinguistic = _paralinguistic_count(markers)
        total_renee = max(1, len(renee_turns))
        laughs = (
            markers.get("laugh", 0)
            + markers.get("soft_laugh", 0)
            + markers.get("suppressed_laugh", 0)
        )
        s = SceneStats(
            scene=scene_num,
            title="",
            renee_turns=len(renee_turns),
            paul_turns=len(paul_turns),
            paralinguistic_count=paralinguistic,
            paralinguistic_per_turn=round(paralinguistic / total_renee, 3),
            warmth=markers.get("warmth", 0),
            quiet=markers.get("quiet", 0),
            dry=markers.get("dry", 0),
            sharp=markers.get("sharp", 0),
            vulnerable=markers.get("vulnerable", 0),
            laughs=laughs,
            sighs=markers.get("sigh", 0),
            breaths=markers.get("breath_in", 0) + markers.get("breath_out", 0),
            beats=markers.get("beat", 0),
            long_beats=markers.get("long_beat", 0),
            callback_marker_hits=_callback_hits(renee_turns),
            mood_label="casual",
        )
        s.mood_label = _scene_mood_label(s)
        stats.append(s)
    return stats


def _vocabulary_texture(renee_turns: list[TurnRecord]) -> dict:
    """Type/token ratio, top lemmas, signature phrases, sensory density."""
    all_text = " ".join(t.text.lower() for t in renee_turns if t.text)
    tokens = re.findall(r"[a-z']+", all_text)
    total = len(tokens)
    types = len(set(tokens))
    ttr = round(types / total, 4) if total else 0.0

    stop = {
        "the", "a", "an", "and", "but", "or", "if", "so", "to", "of", "in", "on",
        "at", "for", "with", "that", "this", "it", "is", "was", "are", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "i", "you",
        "he", "she", "we", "they", "me", "him", "her", "us", "them", "my",
        "your", "our", "their", "his", "its", "not", "no", "yes", "as", "by",
        "from", "about", "into", "like", "just", "there", "here", "what",
        "when", "where", "who", "why", "how",
    }
    content = [t for t in tokens if t not in stop]
    top = Counter(content).most_common(20)

    sig_hits = sum(all_text.count(p) for p in _SIGNATURE_PHRASES)
    sensory_hits = sum(1 for t in tokens if t in _SENSORY_WORDS)
    sensory_density = round(sensory_hits / max(1, total), 4)

    return {
        "tokens": total,
        "types": types,
        "type_token_ratio": ttr,
        "top_content_words": [{"word": w, "count": c} for w, c in top],
        "signature_phrase_hits": sig_hits,
        "signature_phrases_per_turn": round(sig_hits / max(1, len(renee_turns)), 3),
        "sensory_density": sensory_density,
        "sensory_hits": sensory_hits,
    }


def _pause_breakdown(markers: dict[str, int]) -> dict:
    """Proportions of each pause flavor relative to total pause events."""
    beat = markers.get("beat", 0)
    long_beat = markers.get("long_beat", 0)
    trail_off = markers.get("trailing_off", 0)
    breath = markers.get("breath_in", 0) + markers.get("breath_out", 0)
    total = beat + long_beat + trail_off + breath
    def _pct(n: int) -> float:
        return round(n / total, 3) if total else 0.0
    return {
        "total_pause_events": total,
        "beat_ratio": _pct(beat),
        "long_beat_ratio": _pct(long_beat),
        "trailing_ratio": _pct(trail_off),
        "breath_ratio": _pct(breath),
        "combined_breath_beat": breath + beat,
    }


_CALLBACK_ANCHOR_STOPS = {
    # Sentence-initial capitalizations that aren't anchors — conversational
    # exclamations, connectives, short words, and common interrogatives.
    "Yeah", "Okay", "Hey", "Hmm", "What", "When", "Where", "Why", "How",
    "Like", "Which", "That", "This", "These", "Those", "The", "And", "But",
    "Or", "So", "You", "They", "Them", "We", "Us", "Him", "Her", "His",
    "Tell", "More", "Always", "Good", "Sure", "Right", "Fine", "True",
    "Oh", "Ah", "No", "Yes", "Mm", "Because", "Maybe", "Probably",
    "Actually", "Thanks", "Thank", "Something", "Nothing", "Sometimes",
    "First", "Second", "Third", "Next", "Last", "About", "Since", "After",
    "Before", "While", "With", "Without", "From", "Into", "Over", "Under",
    "Just", "Only", "Also",
}


def _callback_structure(all_turns: list[TurnRecord], renee_turns: list[TurnRecord]) -> dict:
    """
    Callback structure. Two signals:
      1. Explicit marker hits in Renée's prose ("you remember", "like you said").
      2. Cross-scene capitalized anchors. Indexed across BOTH speakers so an
         anchor Paul introduces (Marcus in Scene 3) can resolve when Renée
         references it later (Scene 8). We filter out exclamations /
         connectives / question words so the graph highlights real anchors
         (Florence, Marcus, Brunello, Ryan, ...).
    """
    explicit_hits = _callback_hits(renee_turns)

    cap_pat = re.compile(r"\b([A-Z][a-z]{3,})\b")
    by_scene_tokens: dict[int, set[str]] = {}
    for t in all_turns:
        toks = cap_pat.findall(t.text)
        if not toks:
            continue
        by_scene_tokens.setdefault(t.scene, set()).update(
            x for x in toks if x not in _CALLBACK_ANCHOR_STOPS
        )
    anchor_scenes: dict[str, list[int]] = {}
    for scene_num, toks in by_scene_tokens.items():
        for tok in toks:
            anchor_scenes.setdefault(tok, []).append(scene_num)

    cross_scene_anchors = {
        k: sorted(set(v)) for k, v in anchor_scenes.items() if len(set(v)) >= 2
    }
    # If Renée is the one bringing it back, it's a callback she owns.
    renee_tokens_by_scene: dict[int, set[str]] = {}
    for t in renee_turns:
        toks = cap_pat.findall(t.text)
        if not toks:
            continue
        renee_tokens_by_scene.setdefault(t.scene, set()).update(
            x for x in toks if x not in _CALLBACK_ANCHOR_STOPS
        )
    renee_callbacks = []
    for anchor, scenes in cross_scene_anchors.items():
        scenes_with_renee = [
            s for s in scenes if anchor in renee_tokens_by_scene.get(s, set())
        ]
        if scenes_with_renee and scenes_with_renee != scenes:
            # Renée references it in a scene where she didn't originate it.
            intro_scene = min(scenes)
            if intro_scene not in scenes_with_renee:
                renee_callbacks.append({
                    "anchor": anchor,
                    "intro_scene": intro_scene,
                    "recalled_in": scenes_with_renee,
                })
    return {
        "explicit_callback_marker_hits": explicit_hits,
        "cross_scene_anchors": cross_scene_anchors,
        "cross_scene_anchor_count": len(cross_scene_anchors),
        "renee_callbacks": renee_callbacks,
    }


def aggregate(turns: list[TurnRecord]) -> dict:
    renee_turns = [t for t in turns if t.speaker.startswith("REN")]
    paul_turns = [t for t in turns if t.speaker.startswith("PAUL")]
    renee_markers = _marker_counts(renee_turns)
    paralinguistic_total = _paralinguistic_count(renee_markers)
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
    scene_stats = _scene_stats(turns)
    vocab = _vocabulary_texture(renee_turns)
    pause_mix = _pause_breakdown(renee_markers)
    callbacks = _callback_structure(turns, renee_turns)

    mood_arc = [
        {"scene": s.scene, "label": s.mood_label,
         "paralinguistic_per_turn": s.paralinguistic_per_turn}
        for s in scene_stats if s.scene > 0
    ]

    scenes_yaml = [
        {
            "scene": s.scene,
            "mood_label": s.mood_label,
            "renee_turns": s.renee_turns,
            "paul_turns": s.paul_turns,
            "paralinguistic_per_turn": s.paralinguistic_per_turn,
            "warmth": s.warmth,
            "quiet": s.quiet,
            "dry": s.dry,
            "sharp": s.sharp,
            "vulnerable": s.vulnerable,
            "laughs": s.laughs,
            "sighs": s.sighs,
            "breaths": s.breaths,
            "beats": s.beats,
            "long_beats": s.long_beats,
            "callback_marker_hits": s.callback_marker_hits,
        }
        for s in scene_stats if s.scene > 0
    ]

    return {
        "source": "scripts/renee_reference_script.md",
        "extracted_for": "renee",
        "totals": {
            "turns_total": len(turns),
            "renee_turns": len(renee_turns),
            "paul_turns": len(paul_turns),
            "scenes": len(scenes_yaml),
        },
        "turn_length": {
            "renee": _word_stats(renee_turns),
            "paul": _word_stats(paul_turns),
        },
        "hedge_frequency_renee": hedge_rate,
        "paralinguistics_per_turn_renee": paralinguistic_per_turn,
        "marker_counts_renee": renee_markers,
        "pause_markers_renee": pause_markers,
        "pause_distribution_renee": pause_mix,
        "register_markers_renee": register_markers,
        "false_start_count_renee": false_starts,
        "false_start_rate_renee": round(false_starts / max(1, len(renee_turns)), 3),
        "silent_response_count_renee": silent_count,
        "vocabulary_texture_renee": vocab,
        "callbacks_renee": callbacks,
        "mood_arc_renee": mood_arc,
        "scenes_renee": scenes_yaml,
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
    print(f"wrote {out_path} ({data['totals']['turns_total']} turns parsed, "
          f"{data['totals']['scenes']} scenes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
