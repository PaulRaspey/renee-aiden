"""
Paralinguistic injection engine (M6).

Takes the text a persona is about to say, the current mood, and the turn
context, and decides which pre-recorded paralinguistic clips to splice in.
All decisions happen before the TTS call so the prosody layer can request
either pre-synthesis tags (Approach A in architecture/04_paralinguistics.md)
or post-synthesis splice points (Approach B).

The injector is deterministic given its random seed; you can share a seed
across a session to reproduce behavior from telemetry.

The hard rule (architecture/04_paralinguistics.md):
    No paralinguistics during disagreement, correction, hard-truth delivery,
    or user distress.

Public surface:
    ParalinguisticInjector(library_root, mood_axes_source=...)
    injector.plan(text, mood, context) -> list[Injection]
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml


# ---------------------------------------------------------------------------
# data types
# ---------------------------------------------------------------------------

POSITION_START = 0
POSITION_END = -1


@dataclass
class TurnContext:
    """What the persona orchestrator knows about the turn about to be spoken."""
    is_vulnerable_admission: bool = False
    is_witty_callback: bool = False
    is_disagreement: bool = False
    is_correction: bool = False
    is_hard_truth: bool = False
    user_distressed: bool = False
    user_confused_repeatedly: bool = False
    turn_complexity: float = 0.0          # 0..1, where 1 is a thinking-out-loud turn
    conversation_tone: str = "casual"     # casual|playful|serious|vulnerable|heated

    def blocks_paralinguistics(self) -> bool:
        return (
            self.is_disagreement
            or self.is_correction
            or self.is_hard_truth
            or self.user_distressed
            or self.conversation_tone == "heated"
        )


@dataclass
class MoodLike:
    """Duck-typed subset of persona.mood.MoodState needed by the injector."""
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
class Injection:
    category: str
    subcategory: str
    position: int            # character offset, or POSITION_START / POSITION_END
    intensity: float
    clip_path: Optional[Path] = None
    clip_meta: dict = field(default_factory=dict)
    reason: str = ""


# ---------------------------------------------------------------------------
# density table (per architecture/04_paralinguistics.md)
# ---------------------------------------------------------------------------

DENSITY_PER_TURN = {
    "casual": 0.40,      # 1 per ~2.5 turns
    "playful": 0.80,     # ~1 per turn
    "serious": 0.15,     # 1 per ~6 turns
    "vulnerable": 0.50,  # ~1 every other turn, mostly breaths + soft reactions
    "heated": 0.00,      # none
}


# ---------------------------------------------------------------------------
# clip library
# ---------------------------------------------------------------------------

class ClipLibrary:
    """
    Loads paralinguistics/<voice>/metadata.yaml and groups clips by
    (category, subcategory). Resolves absolute paths on disk and drops entries
    whose files are missing.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.clips: dict[tuple[str, str], list[dict]] = {}
        self.metadata_path = self.root / "metadata.yaml"
        self._load()

    def _load(self) -> None:
        if not self.metadata_path.exists():
            return
        raw = yaml.safe_load(self.metadata_path.read_text(encoding="utf-8")) or {}
        for entry in raw.get("clips", []):
            rel = entry.get("file")
            if not rel:
                continue
            path = self.root / rel
            if not path.exists():
                continue
            key = (entry.get("category", ""), entry.get("subcategory", ""))
            enriched = dict(entry)
            enriched["_abs"] = path
            self.clips.setdefault(key, []).append(enriched)

    def get(self, category: str, subcategory: str) -> list[dict]:
        return self.clips.get((category, subcategory), [])

    def categories(self) -> list[tuple[str, str]]:
        return list(self.clips.keys())

    def __len__(self) -> int:
        return sum(len(v) for v in self.clips.values())


# ---------------------------------------------------------------------------
# injector
# ---------------------------------------------------------------------------

class ParalinguisticInjector:
    """
    Rule engine + selector. One instance per persona for the lifetime of a
    conversation; call `plan(...)` per utterance.
    """

    def __init__(
        self,
        library_root: Path,
        *,
        max_per_turn: int = 2,
        recency_seconds: int = 120,
        recent_clip_window: int = 10,
        rng: Optional[random.Random] = None,
    ):
        self.library = ClipLibrary(library_root)
        self.max_per_turn = max_per_turn
        self.recency_seconds = recency_seconds
        self.recent_clip_window = recent_clip_window
        self.rng = rng or random.Random()
        # deque of (file, timestamp) for recency and clip-in-window filters
        self._recent_clips: deque[tuple[str, float]] = deque()
        # deque of (category/subcategory, timestamp)
        self._recent_categories: deque[tuple[str, float]] = deque()

    # ------------------------------------------------------------------
    # rule engine
    # ------------------------------------------------------------------

    def plan(self, text: str, mood: Any, context: TurnContext) -> list[Injection]:
        if not text:
            return []
        m = MoodLike.from_obj(mood)
        now = time.time()
        self._prune_recency(now)

        if context.blocks_paralinguistics():
            return []

        mandatory = self._propose_mandatory(text, m, context)
        ornamental = self._propose_ornamental(text, m, context)

        density = DENSITY_PER_TURN.get(context.conversation_tone, 0.4)
        if self.rng.random() > max(density, 0.0):
            ornamental = []

        proposed = mandatory + ornamental
        proposed = self._deduplicate(proposed)
        proposed = self._mood_filter(proposed, m)
        proposed = self._frequency_cap(proposed)
        proposed = self._bind_clips(proposed, m, now)
        proposed = [inj for inj in proposed if inj.clip_path is not None]
        return proposed

    def _propose_mandatory(self, text: str, m: MoodLike, ctx: TurnContext) -> list[Injection]:
        """Semantic injections that are part of the meaning of the turn.

        These bypass the density gate because they are load-bearing: skipping
        them leaves the delivery flat in exactly the moments the paralinguistic
        layer exists to serve.
        """
        injections: list[Injection] = []

        # Vulnerable admission gets a preceding sharp inhale.
        if ctx.is_vulnerable_admission:
            injections.append(Injection(
                category="breaths", subcategory="sharp_in",
                position=POSITION_START, intensity=0.3,
                reason="vulnerable_admission",
            ))

        # Thinking pause before a complex answer.
        if ctx.turn_complexity > 0.7:
            injections.append(Injection(
                category="thinking", subcategory="mm",
                position=POSITION_START, intensity=0.3,
                reason="high_complexity",
            ))

        # Repeated user confusion + low patience -> frustrated sigh.
        if ctx.user_confused_repeatedly and m.patience < 0.4:
            injections.append(Injection(
                category="sighs", subcategory="frustrated",
                position=POSITION_START, intensity=0.4,
                reason="repeated_confusion",
            ))

        return injections

    def _propose_ornamental(self, text: str, m: MoodLike, ctx: TurnContext) -> list[Injection]:
        """Stylistic injections that the density gate may drop."""
        injections: list[Injection] = []

        # Witty callback + playful mood lands with a soft laugh.
        if ctx.is_witty_callback and m.playfulness > 0.6:
            injections.append(Injection(
                category="laughs", subcategory="soft",
                position=POSITION_END,
                intensity=min(0.5, m.playfulness),
                reason="witty_callback",
            ))

        # Tired mood + long text -> tired sigh at start (soft).
        if m.energy < 0.35 and len(text.split()) > 25 and ctx.conversation_tone != "heated":
            injections.append(Injection(
                category="sighs", subcategory="tired",
                position=POSITION_START, intensity=0.3,
                reason="low_energy_long_turn",
            ))

        # Exclamations + playful mood -> amusement reaction.
        if ("!" in text or "?!" in text) and m.playfulness > 0.7 and ctx.conversation_tone in ("playful", "casual"):
            injections.append(Injection(
                category="reactions", subcategory="amusement",
                position=POSITION_START, intensity=0.4,
                reason="exclaim_playful",
            ))

        return injections

    # ------------------------------------------------------------------
    # filters
    # ------------------------------------------------------------------

    def _deduplicate(self, injections: list[Injection]) -> list[Injection]:
        seen: set[tuple[str, str]] = set()
        out: list[Injection] = []
        for inj in injections:
            key = (inj.category, inj.subcategory)
            if key in seen:
                continue
            seen.add(key)
            out.append(inj)
        return out

    def _mood_filter(self, injections: list[Injection], mood: MoodLike) -> list[Injection]:
        out: list[Injection] = []
        for inj in injections:
            if inj.category == "reactions" and mood.energy < 0.3 and inj.subcategory == "amusement":
                continue  # tired -> no amusement
            if inj.category == "laughs" and inj.subcategory == "hearty" and mood.energy < 0.5:
                inj.subcategory = "soft"  # downshift
            out.append(inj)
        return out

    def _frequency_cap(self, injections: list[Injection]) -> list[Injection]:
        return injections[: self.max_per_turn]

    # ------------------------------------------------------------------
    # selection
    # ------------------------------------------------------------------

    def _bind_clips(self, injections: list[Injection], mood: MoodLike, now: float) -> list[Injection]:
        for inj in injections:
            clip = self._pick_clip(inj, mood, now)
            if clip is None:
                continue
            inj.clip_path = clip["_abs"]
            inj.clip_meta = {k: v for k, v in clip.items() if not k.startswith("_")}
            self._recent_clips.append((str(clip["_abs"]), now))
            self._recent_categories.append((f"{inj.category}/{inj.subcategory}", now))
        return injections

    def _pick_clip(self, inj: Injection, mood: MoodLike, now: float) -> Optional[dict]:
        candidates = self.library.get(inj.category, inj.subcategory)
        if not candidates:
            # downgrade same-category-any-sub if subcategory missing
            for (cat, sub), clips in self.library.clips.items():
                if cat == inj.category and clips:
                    candidates = clips
                    break
        if not candidates:
            return None

        recent_paths = {p for p, _ in self._recent_clips}
        recent_window = list(self._recent_clips)[-self.recent_clip_window:]
        recent_window_paths = {p for p, _ in recent_window}

        def score(clip: dict) -> float:
            s = 0.0
            intensity = float(clip.get("intensity", 0.5))
            s -= abs(intensity - inj.intensity) * 1.5
            energy = float(clip.get("energy_level", 0.5))
            s -= abs(energy - mood.energy) * 0.8
            path = str(clip["_abs"])
            if path in recent_window_paths:
                s -= 2.5
            if path in recent_paths:
                s -= 0.3
            # tiny random jitter so identical scores pick differently
            s += self.rng.uniform(0.0, 0.05)
            return s

        # Filter out clips whose `inappropriate_contexts` conflict with mood tags
        filtered = [c for c in candidates if self._mood_allows_clip(c, mood)]
        pool = filtered or candidates
        return max(pool, key=score)

    def _mood_allows_clip(self, clip: dict, mood: MoodLike) -> bool:
        inappropriate = set(clip.get("inappropriate_contexts", []) or [])
        # very low energy rejects high-energy clips
        if mood.energy < 0.3 and float(clip.get("energy_level", 0.5)) > 0.75:
            return False
        # very high patience rejects frustrated clips
        if mood.patience > 0.8 and "frustrated" in " ".join(clip.get("tags", []) or []):
            return False
        # vulnerable mood rejects anything tagged high_energy
        if mood.warmth > 0.85 and "high_energy" in (clip.get("tags") or []):
            return False
        return True

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _prune_recency(self, now: float) -> None:
        cutoff = now - self.recency_seconds
        while self._recent_clips and self._recent_clips[0][1] < cutoff:
            self._recent_clips.popleft()
        while self._recent_categories and self._recent_categories[0][1] < cutoff:
            self._recent_categories.popleft()

    # expose for tests/debugging
    def recent_clips(self) -> list[str]:
        return [p for p, _ in self._recent_clips]

    def library_size(self) -> int:
        return len(self.library)
