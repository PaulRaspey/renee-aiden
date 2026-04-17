"""
Prosody layer (M7).

Takes a persona text output, the current mood, and the turn context, and
produces a structured plan: rate, base pitch, sentence pauses, per-sentence
pitch contours, paralinguistic injections, and vocal-effect flags.

The plan is the input to TTS. On XTTS-v2 we serialize to an SSML-like
markup (approach A in architecture/04_paralinguistics.md) and emit splice
points for pre-recorded paralinguistic clips (approach B).

Rules live in `configs/prosody_rules.yaml`.

Hard rules enforced here:
  - A vulnerable admission ALWAYS gets a `sharp_in` breath directly before
    it. Structural to the voice. Fires even when other paralinguistic
    output is suppressed, because the breath IS the admission's preamble.
  - No ornamental paralinguistics and no vocal effects during
    disagreement, correction, hard-truth delivery, user distress, or a
    heated conversation tone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = REPO_ROOT / "configs" / "prosody_rules.yaml"

PARALINGUISTIC_KINDS = {"breath", "laugh", "sigh", "thinking", "reaction"}

# Map paralinguistic injector categories to prosody-layer kinds.
INJECTOR_CATEGORY_TO_KIND = {
    "breaths": "breath",
    "laughs": "laugh",
    "sighs": "sigh",
    "thinking": "thinking",
    "reactions": "reaction",
    "affirmations": "reaction",
    "fillers": "reaction",
}


# ---------------------------------------------------------------------------
# data types
# ---------------------------------------------------------------------------


@dataclass
class ProsodyContext:
    """What the orchestrator knows about the turn for prosody shaping."""
    is_question: bool = False
    is_callback: bool = False
    is_vulnerable_admission: bool = False
    is_emotional_beat: bool = False
    is_disagreement: bool = False
    is_correction: bool = False
    is_hard_truth: bool = False
    user_distressed: bool = False
    conversation_tone: str = "casual"   # casual|playful|serious|vulnerable|heated
    turn_role: str = "response"         # greeting|response|question|callback|closer

    def blocks_effects(self) -> bool:
        return (
            self.is_disagreement
            or self.is_correction
            or self.is_hard_truth
            or self.user_distressed
            or self.conversation_tone == "heated"
        )


@dataclass
class MoodLike:
    """Duck-typed subset of persona.mood.MoodState used by the planner."""
    energy: float = 0.65
    warmth: float = 0.75
    playfulness: float = 0.70
    focus: float = 0.75
    patience: float = 0.65
    curiosity: float = 0.80

    @classmethod
    def from_obj(cls, obj: Any) -> "MoodLike":
        if isinstance(obj, MoodLike):
            return obj
        return cls(
            energy=float(getattr(obj, "energy", 0.65)),
            warmth=float(getattr(obj, "warmth", 0.75)),
            playfulness=float(getattr(obj, "playfulness", 0.70)),
            focus=float(getattr(obj, "focus", 0.75)),
            patience=float(getattr(obj, "patience", 0.65)),
            curiosity=float(getattr(obj, "curiosity", 0.80)),
        )


@dataclass
class ProsodySegment:
    """One unit in the plan: text, pause, or paralinguistic tag."""
    kind: str                           # 'text'|'pause'|'breath'|'laugh'|'sigh'|'thinking'|'reaction'
    content: str = ""
    duration_ms: int = 0
    category: str = ""                  # original injector category, if sourced from M6
    subcategory: str = ""
    intensity: float = 0.0
    pitch_delta: float = 0.0            # for text segments
    clip_path: Optional[str] = None     # set when bound to paralinguistic library
    reason: str = ""


@dataclass
class ProsodyPlan:
    text: str
    segments: list[ProsodySegment]
    rate: float
    pitch_base: float
    emotion: str
    effects: list[str]
    reasons: list[str] = field(default_factory=list)

    def paralinguistic_count(self) -> int:
        return sum(1 for s in self.segments if s.kind in PARALINGUISTIC_KINDS)

    def to_ssml(self) -> str:
        attrs = [
            f'emotion="{self.emotion}"',
            f'rate="{self.rate:.2f}"',
            f'pitch="{_fmt_pitch(self.pitch_base)}"',
        ]
        if self.effects:
            attrs.append(f'effects="{",".join(self.effects)}"')
        lines = [f"<speak {' '.join(attrs)}>"]
        for seg in self.segments:
            rendered = _segment_to_ssml(seg)
            if rendered:
                lines.append(rendered)
        lines.append("</speak>")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "rate": self.rate,
            "pitch_base": self.pitch_base,
            "emotion": self.emotion,
            "effects": list(self.effects),
            "reasons": list(self.reasons),
            "segments": [
                {
                    "kind": s.kind,
                    "content": s.content,
                    "duration_ms": s.duration_ms,
                    "category": s.category,
                    "subcategory": s.subcategory,
                    "intensity": s.intensity,
                    "pitch_delta": s.pitch_delta,
                    "clip_path": s.clip_path,
                    "reason": s.reason,
                }
                for s in self.segments
            ],
        }


# ---------------------------------------------------------------------------
# ssml helpers
# ---------------------------------------------------------------------------


def _fmt_pitch(p: float) -> str:
    if abs(p) < 1e-6:
        return "0%"
    sign = "+" if p > 0 else ""
    return f"{sign}{int(round(p * 100))}%"


def _segment_to_ssml(s: ProsodySegment) -> str:
    if s.kind == "text":
        content = s.content.strip()
        if not content:
            return ""
        if abs(s.pitch_delta) > 1e-6:
            return f'  <prosody pitch="{_fmt_pitch(s.pitch_delta)}">{content}</prosody>'
        return f"  {content}"
    if s.kind == "pause":
        return f'  <pause duration="{s.duration_ms}"/>'
    if s.kind in PARALINGUISTIC_KINDS:
        tag_map = {
            "breath": "breath",
            "laugh": "laugh",
            "sigh": "sigh",
            "thinking": "thinking",
            "reaction": "reaction",
        }
        tag = tag_map[s.kind]
        bits = []
        if s.subcategory:
            bits.append(f'type="{s.subcategory}"')
        if s.intensity:
            bits.append(f'intensity="{s.intensity:.2f}"')
        rest = (" " + " ".join(bits)) if bits else ""
        return f"  <{tag}{rest}/>"
    return ""


# ---------------------------------------------------------------------------
# sentence segmentation
# ---------------------------------------------------------------------------


_SENT_SPLIT = re.compile(r"([.!?]+|\n+)")


def segment_sentences(text: str) -> list[tuple[str, str]]:
    """Split into (body, end_punct). Whitespace-trim bodies; drop empties."""
    if not text or not text.strip():
        return []
    parts = _SENT_SPLIT.split(text)
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        body = parts[i].strip()
        punct = parts[i + 1].strip() if i + 1 < len(parts) else ""
        # newlines don't become punctuation — they just break sentences
        if punct and not any(c in ".!?" for c in punct):
            punct = ""
        if body:
            out.append((body, punct))
        i += 2
    return out


# ---------------------------------------------------------------------------
# default rules
# ---------------------------------------------------------------------------


DEFAULT_RULES: dict = {
    "global": {"default_rate": 1.0, "default_pitch": 0.0},
    "rate_modulation": {
        "energy_floor": 0.85,
        "energy_ceiling": 1.15,
        "serious_modifier": 0.93,
        "playful_modifier": 1.06,
    },
    "pause_rules": {
        "comma_ms": 150,
        "period_ms_base": 400,
        "period_ms_by_mood": {
            "low_energy": 550,
            "high_energy": 280,
            "thoughtful": 500,
        },
        "dramatic_before_emotional": 1200,
        "dramatic_before_callback": 300,
        "thinking_mid_turn": 600,
    },
    "pitch_contour": {
        "question_rise": 0.15,
        "statement_fall": -0.08,
        "confident_flat": 0.0,
        "vulnerable_soft": -0.12,
        "callback_lift": 0.05,
    },
    "vocal_effects": {
        "creak_on_low_energy": True,
        "breathiness_on_intimate": True,
        "reduce_all_effects_if_distressed": True,
    },
    "paralinguistic_density": {
        "casual": 0.4,
        "playful": 0.8,
        "serious": 0.1,
        "vulnerable": 0.5,
        "heated": 0.0,
    },
    "breath_rules": {
        "sharp_in_before_vulnerable": True,
        "slow_out_on_acceptance": True,
        "thinking_in_before_complex": 0.3,
    },
    "constraints": {
        "max_paralinguistics_per_turn": 2,
        "min_time_between_same_category": 120,
        "no_paralinguistics_during": [
            "disagreement",
            "correction",
            "hard_truth_delivery",
            "user_distress",
        ],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Dict merge: override wins; nested dicts merged recursively."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_rules(path: Optional[Path | str] = None) -> dict:
    path = Path(path) if path else DEFAULT_RULES_PATH
    if not path.exists():
        return dict(DEFAULT_RULES)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _deep_merge(DEFAULT_RULES, raw)


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------


class ProsodyPlanner:
    """
    One instance per persona; call plan() per utterance. Stateless between
    calls — state is carried by the paralinguistic injector.
    """

    def __init__(self, rules_path: Optional[Path | str] = None, *, rules: Optional[dict] = None):
        self.rules = rules if rules is not None else load_rules(rules_path)

    # ------------------------------------------------------------------

    def plan(
        self,
        text: str,
        mood: Any,
        context: ProsodyContext,
        injections: Optional[Iterable[Any]] = None,
    ) -> ProsodyPlan:
        m = MoodLike.from_obj(mood)

        rate = self._compute_rate(m, context)
        pitch_base = self._compute_base_pitch(m, context)
        emotion = self._compute_emotion(m, context)
        effects = self._compute_effects(m, context)

        reasons: list[str] = []
        segments: list[ProsodySegment] = []

        # Convert injector output into prosody segments, split by position.
        blocks = context.blocks_effects()
        injected_start, injected_end = self._split_injections(
            injections, blocks_ornamental=blocks
        )

        # HARD RULE — vulnerable admission always gets a sharp inhale at start.
        if context.is_vulnerable_admission:
            already = any(
                seg.kind == "breath" and seg.subcategory in ("sharp_in", "in")
                for seg in injected_start
            )
            if not already:
                injected_start.insert(
                    0,
                    ProsodySegment(
                        kind="breath",
                        category="breaths",
                        subcategory="sharp_in",
                        intensity=0.3,
                        reason="vulnerable_admission_hard_rule",
                    ),
                )
                reasons.append("hard_rule: inserted sharp_in before vulnerable admission")

        segments.extend(injected_start)

        # Dramatic pre-pause, if the turn warrants one.
        pre_pause = self._pre_pause_ms(context)
        if pre_pause > 0:
            segments.append(
                ProsodySegment(
                    kind="pause",
                    duration_ms=pre_pause,
                    reason=self._pre_pause_reason(context),
                )
            )

        # Sentences + inter-sentence pauses + per-sentence pitch contour.
        sentences = segment_sentences(text)
        sentence_pause_ms = self._sentence_pause_ms(m, context)
        for idx, (body, punct) in enumerate(sentences):
            is_q = "?" in punct or (context.is_question and idx == len(sentences) - 1)
            is_last = idx == len(sentences) - 1
            is_first = idx == 0
            pitch_delta = self._sentence_contour(m, context, is_q, is_last, is_first)
            # Insert comma pauses inside the sentence body.
            body_with_commas = self._wrap_commas(body, m)
            segments.append(
                ProsodySegment(
                    kind="text",
                    content=f"{body_with_commas}{punct}",
                    pitch_delta=pitch_delta,
                    reason=f"sentence_{idx}",
                )
            )
            if not is_last:
                segments.append(
                    ProsodySegment(
                        kind="pause",
                        duration_ms=sentence_pause_ms,
                        reason="sentence_pause",
                    )
                )

        segments.extend(injected_end)

        plan = ProsodyPlan(
            text=text,
            segments=segments,
            rate=rate,
            pitch_base=pitch_base,
            emotion=emotion,
            effects=effects,
            reasons=reasons,
        )
        self._enforce_paralinguistic_cap(plan)
        return plan

    # ------------------------------------------------------------------
    # computation
    # ------------------------------------------------------------------

    def _compute_rate(self, m: MoodLike, context: ProsodyContext) -> float:
        r_cfg = self.rules["rate_modulation"]
        floor = r_cfg["energy_floor"]
        ceiling = r_cfg["energy_ceiling"]
        r = floor + (ceiling - floor) * m.energy

        tone = context.conversation_tone
        if tone in ("serious", "vulnerable"):
            r *= r_cfg["serious_modifier"]
        elif tone == "playful":
            r *= r_cfg["playful_modifier"]

        # Mood secondary influences.
        if m.playfulness > 0.8 and tone != "heated":
            r *= 1.02
        if m.focus < 0.35:
            r *= 1.05   # scattered = faster, choppier
        if context.is_vulnerable_admission:
            r *= 0.94   # slow down for vulnerable beats
        if context.is_emotional_beat:
            r *= 0.95

        return round(max(0.75, min(1.30, r)), 3)

    def _compute_base_pitch(self, m: MoodLike, context: ProsodyContext) -> float:
        contour = self.rules["pitch_contour"]
        p = 0.0
        if context.conversation_tone == "vulnerable" or context.is_vulnerable_admission:
            p += contour["vulnerable_soft"]
        if m.energy < 0.4:
            p -= 0.04
        if context.is_hard_truth:
            p -= 0.03
        return round(max(-0.30, min(0.30, p)), 3)

    def _compute_emotion(self, m: MoodLike, context: ProsodyContext) -> str:
        if context.is_disagreement or context.is_correction:
            return "firm"
        if context.is_hard_truth:
            return "grave"
        if context.user_distressed:
            return "tender"
        if context.is_vulnerable_admission:
            return "vulnerable"
        tone = context.conversation_tone
        if tone == "playful":
            return "playful"
        if tone == "serious":
            return "thoughtful"
        if tone == "vulnerable":
            return "vulnerable"
        if tone == "heated":
            return "intense"
        if m.warmth > 0.8:
            return "warm"
        if m.energy < 0.35:
            return "tired"
        if m.playfulness > 0.75:
            return "playful"
        return "neutral"

    def _compute_effects(self, m: MoodLike, context: ProsodyContext) -> list[str]:
        ve = self.rules["vocal_effects"]
        if ve.get("reduce_all_effects_if_distressed") and (
            context.user_distressed or context.blocks_effects()
        ):
            return []
        effects: list[str] = []
        if ve.get("creak_on_low_energy") and m.energy < 0.4:
            effects.append("creak")
        if ve.get("breathiness_on_intimate"):
            intimate = (
                context.conversation_tone == "vulnerable"
                or context.is_vulnerable_admission
                or (m.warmth > 0.85 and m.energy < 0.55)
            )
            if intimate:
                effects.append("breathy")
        return effects

    # ------------------------------------------------------------------
    # pauses & contour
    # ------------------------------------------------------------------

    def _pre_pause_ms(self, context: ProsodyContext) -> int:
        pr = self.rules["pause_rules"]
        pre = 0
        if context.is_emotional_beat:
            pre = max(pre, int(pr.get("dramatic_before_emotional", 1200)))
        if context.is_callback:
            pre = max(pre, int(pr.get("dramatic_before_callback", 300)))
        return pre

    def _pre_pause_reason(self, context: ProsodyContext) -> str:
        if context.is_emotional_beat:
            return "dramatic_before_emotional"
        if context.is_callback:
            return "dramatic_before_callback"
        return "pre_pause"

    def _sentence_pause_ms(self, m: MoodLike, context: ProsodyContext) -> int:
        pr = self.rules["pause_rules"]
        by_mood = pr.get("period_ms_by_mood", {})
        if m.energy < 0.4:
            return int(by_mood.get("low_energy", 550))
        if m.energy > 0.75 and context.conversation_tone in ("casual", "playful"):
            return int(by_mood.get("high_energy", 280))
        if context.conversation_tone in ("serious", "vulnerable") and m.focus > 0.7:
            return int(by_mood.get("thoughtful", 500))
        return int(pr.get("period_ms_base", 400))

    def _sentence_contour(
        self,
        m: MoodLike,
        context: ProsodyContext,
        is_question_sent: bool,
        is_last: bool,
        is_first: bool,
    ) -> float:
        c = self.rules["pitch_contour"]
        p = 0.0
        if is_question_sent:
            p += float(c["question_rise"])
        elif is_last:
            # Statement fall only on the last sentence of a confident turn.
            if not context.is_vulnerable_admission and context.conversation_tone != "vulnerable":
                p += float(c["statement_fall"])
        if context.is_callback and is_first:
            p += float(c["callback_lift"])
        return round(p, 3)

    def _wrap_commas(self, body: str, m: MoodLike) -> str:
        """
        Minimal comma markup: for text segments longer than ~12 words with
        commas, produce a softened spelling ", " that the TTS layer may
        honor as a micro-pause. XTTS-v2 already respects commas natively,
        so we keep this conservative.
        """
        # No transformation needed — leave commas in place; TTS and downstream
        # prosody compile honor the `comma_ms` rule by reading this body.
        return body

    # ------------------------------------------------------------------
    # paralinguistic bookkeeping
    # ------------------------------------------------------------------

    def _split_injections(
        self,
        injections: Optional[Iterable[Any]],
        *,
        blocks_ornamental: bool,
    ) -> tuple[list[ProsodySegment], list[ProsodySegment]]:
        start: list[ProsodySegment] = []
        end: list[ProsodySegment] = []
        if not injections:
            return start, end
        for inj in injections:
            seg = self._injection_to_segment(inj)
            if seg is None:
                continue
            # If the turn is blocked (disagreement/etc), drop ornamental injections.
            # The vulnerable-admission breath is re-inserted later by the hard rule
            # independent of injector output.
            if blocks_ornamental:
                continue
            pos = getattr(inj, "position", 0)
            if pos == -1:
                end.append(seg)
            else:
                start.append(seg)
        return start, end

    def _injection_to_segment(self, inj: Any) -> Optional[ProsodySegment]:
        category = getattr(inj, "category", "")
        kind = INJECTOR_CATEGORY_TO_KIND.get(category)
        if kind is None:
            return None
        clip_path = getattr(inj, "clip_path", None)
        return ProsodySegment(
            kind=kind,
            category=category,
            subcategory=getattr(inj, "subcategory", ""),
            intensity=float(getattr(inj, "intensity", 0.0) or 0.0),
            clip_path=str(clip_path) if clip_path else None,
            reason=getattr(inj, "reason", "") or f"inj_{category}",
        )

    def _enforce_paralinguistic_cap(self, plan: ProsodyPlan) -> None:
        cap = int(self.rules.get("constraints", {}).get("max_paralinguistics_per_turn", 2))
        paras = [(i, s) for i, s in enumerate(plan.segments) if s.kind in PARALINGUISTIC_KINDS]
        if len(paras) <= cap:
            return
        # Preserve the mandatory vulnerable-admission breath first.
        to_keep: set[int] = set()
        for i, s in paras:
            if s.reason == "vulnerable_admission_hard_rule":
                to_keep.add(i)
        for i, _ in paras:
            if len(to_keep) >= cap:
                break
            to_keep.add(i)
        dropped = 0
        new_segments: list[ProsodySegment] = []
        for i, s in enumerate(plan.segments):
            if s.kind in PARALINGUISTIC_KINDS and i not in to_keep:
                dropped += 1
                continue
            new_segments.append(s)
        if dropped:
            plan.segments = new_segments
            plan.reasons.append(f"paralinguistic_cap: trimmed {dropped} over cap {cap}")
