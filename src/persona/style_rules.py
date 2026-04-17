"""
Style-reference loader + persona/prosody rule derivation (M12).

The style extractor writes `configs/style_reference.yaml`. This module
reads it, exposes a structured `StyleReference`, and derives:

  - A concise style-constraint block for the system prompt, summarizing
    turn length, hedge rate, paralinguistic density, signature phrases,
    and false-start cadence.
  - A `paralinguistic_density_by_tone` mapping keyed on conversation tone
    (casual / playful / serious / vulnerable / heated) drawn from the
    per-scene mood_arc. These override the default density targets in
    `configs/prosody_rules.yaml` when a style reference is present.

Load points:
  - `src/persona/core.py` reads the reference on construction and passes
    it to the prompt assembler via `build_system_prompt(style_reference=...)`.
  - `src/orchestrator.py` pipes the same reference into `ProsodyPlanner`
    so the density gate honors measured paralinguistic rates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# Map scene mood labels to prosody conversation-tone keys.
MOOD_LABEL_TO_TONE = {
    "casual": "casual",
    "light": "casual",       # light-hearted small talk is casual tone
    "serious": "serious",
    "intimate": "vulnerable",
    "conflict": "heated",
}


@dataclass
class StyleReference:
    source_path: str
    extracted_for: str
    turn_length_median: int
    turn_length_mean: float
    turn_length_p95: int
    hedge_frequency: float
    paralinguistics_per_turn: float
    false_start_rate: float
    silent_response_count: int
    signature_phrase_count: int
    signature_phrases_per_turn: float
    sensory_density: float
    type_token_ratio: float
    top_content_words: list[str] = field(default_factory=list)
    pause_distribution: dict = field(default_factory=dict)
    register_markers: dict = field(default_factory=dict)
    mood_arc: list[dict] = field(default_factory=list)
    scenes: list[dict] = field(default_factory=list)
    callback_anchors: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    # -------- derived helpers --------

    def paralinguistic_density_by_tone(self) -> dict[str, float]:
        """Average paralinguistic_per_turn grouped by mapped conversation tone."""
        buckets: dict[str, list[float]] = {}
        for entry in self.mood_arc:
            label = entry.get("label", "casual")
            tone = MOOD_LABEL_TO_TONE.get(label, "casual")
            rate = float(entry.get("paralinguistic_per_turn", 0.0))
            buckets.setdefault(tone, []).append(rate)
        out: dict[str, float] = {}
        for tone, rates in buckets.items():
            if rates:
                out[tone] = round(sum(rates) / len(rates), 3)
        return out

    def prompt_style_block(self) -> str:
        """
        A short constraint block embedded in the system prompt so the LLM
        actually matches the measured reference cadence.
        """
        tl_median = self.turn_length_median
        tl_p95 = self.turn_length_p95
        hedge_pct = int(round(self.hedge_frequency * 100))
        para_rate = self.paralinguistics_per_turn
        fs_pct = int(round(self.false_start_rate * 100))

        # Trim top words to frequent non-exclamatory content words the persona
        # tends to reach for. We surface only up to five so we shape without
        # dictating.
        hot = [w for w in self.top_content_words[:12] if len(w) > 2][:5]

        sig_phrases = ["yeah no", "honestly though", "okay but", "kind of", "sort of"]

        lines = [
            "STYLE CONSTRAINTS (measured from reference script, not arbitrary):",
            f"  - Turn length median ~{tl_median} words, p95 ~{tl_p95} words. "
            f"Short by default; expand only when it matters.",
            f"  - Hedge on roughly {hedge_pct}% of factual statements — not every "
            f"sentence needs it.",
            f"  - Paralinguistic density ~{para_rate:.2f} per turn on average. "
            f"Higher in intimate beats, near zero in conflict.",
            f"  - False starts ~{max(fs_pct,1)}% of turns — occasional, not performative.",
            "  - Silent response is a valid choice on heavy beats. Don't always fill space.",
        ]
        if hot:
            lines.append(
                "  - Words she naturally reaches for: " + ", ".join(f'"{w}"' for w in hot) + "."
            )
        lines.append(
            "  - Signature phrases (use sparingly, not back-to-back): "
            + ", ".join(f'"{p}"' for p in sig_phrases) + "."
        )
        if self.callback_anchors:
            anchors = ", ".join(self.callback_anchors[:5])
            lines.append(
                f"  - Callbacks known to land: {anchors}. Bring them back when earned."
            )
        return "\n".join(lines)


def load_style_reference(path: str | Path) -> Optional[StyleReference]:
    p = Path(path)
    if not p.exists():
        return None
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return None

    tl = (raw.get("turn_length") or {}).get("renee") or {}
    vocab = raw.get("vocabulary_texture_renee") or {}
    pause = raw.get("pause_distribution_renee") or {}
    registers = raw.get("register_markers_renee") or {}
    mood_arc = raw.get("mood_arc_renee") or []
    scenes = raw.get("scenes_renee") or []
    callbacks = raw.get("callbacks_renee") or {}
    anchors_raw = callbacks.get("cross_scene_anchors") or {}
    # Rank anchors by how many scenes they span.
    anchors = sorted(
        anchors_raw.items(),
        key=lambda kv: (-len(kv[1] if isinstance(kv[1], list) else []), kv[0]),
    )
    anchor_names = [k for k, _ in anchors]

    top_words = [
        entry["word"]
        for entry in (vocab.get("top_content_words") or [])
        if isinstance(entry, dict) and entry.get("word")
    ]

    return StyleReference(
        source_path=raw.get("source", str(p)),
        extracted_for=raw.get("extracted_for", "renee"),
        turn_length_median=int(tl.get("words_median", 0) or 0),
        turn_length_mean=float(tl.get("words_mean", 0.0) or 0.0),
        turn_length_p95=int(tl.get("words_p95", 0) or 0),
        hedge_frequency=float(raw.get("hedge_frequency_renee", 0.0) or 0.0),
        paralinguistics_per_turn=float(raw.get("paralinguistics_per_turn_renee", 0.0) or 0.0),
        false_start_rate=float(raw.get("false_start_rate_renee", 0.0) or 0.0),
        silent_response_count=int(raw.get("silent_response_count_renee", 0) or 0),
        signature_phrase_count=int(vocab.get("signature_phrase_hits", 0) or 0),
        signature_phrases_per_turn=float(vocab.get("signature_phrases_per_turn", 0.0) or 0.0),
        sensory_density=float(vocab.get("sensory_density", 0.0) or 0.0),
        type_token_ratio=float(vocab.get("type_token_ratio", 0.0) or 0.0),
        top_content_words=top_words,
        pause_distribution=dict(pause),
        register_markers=dict(registers),
        mood_arc=list(mood_arc),
        scenes=list(scenes),
        callback_anchors=anchor_names,
        raw=raw,
    )
