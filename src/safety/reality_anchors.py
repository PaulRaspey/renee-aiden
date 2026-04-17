"""
Reality-anchor injector (M13 / SAFETY.md §Reality Anchors).

Occasionally weaves a soft acknowledgement of Renée's nature into a turn.
Rate target: ~1 in 50 turns. Suppressed during disagreement, correction,
hard truth, user distress, or the opening of a vulnerable admission —
those moments are load-bearing and shouldn't be interrupted.

Usage:
    injector = RealityAnchorInjector.from_config(cfg, rng=seeded_rng)
    result = injector.maybe_inject(response_text, turn_number, ctx_flags)
    # result.injected is True/False; result.text is the (possibly) updated response.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import RealityAnchorsConfig


@dataclass
class AnchorResult:
    text: str
    injected: bool = False
    phrase: str = ""
    reason: str = ""


class RealityAnchorInjector:
    """
    Probabilistic reality-anchor injector. Deterministic given an RNG seed —
    `RealityAnchorInjector(rng=random.Random(42))` replays identically. That
    makes eval-harness comparisons stable.
    """

    def __init__(
        self,
        *,
        rate_denominator: int = 50,
        min_turn_gap: int = 8,
        phrases: Optional[Iterable[str]] = None,
        suppress_when_any_of: Optional[Iterable[str]] = None,
        rng: Optional[random.Random] = None,
        enabled: bool = True,
    ):
        self.rate_denominator = max(1, int(rate_denominator))
        self.min_turn_gap = max(0, int(min_turn_gap))
        self.phrases = list(phrases or [])
        self.suppress_flags = set(suppress_when_any_of or [])
        self._rng = rng or random.Random()
        self._last_fire_turn: Optional[int] = None
        self.enabled = enabled

    @classmethod
    def from_config(
        cls, cfg: RealityAnchorsConfig, rng: Optional[random.Random] = None
    ) -> "RealityAnchorInjector":
        return cls(
            rate_denominator=cfg.rate_denominator,
            min_turn_gap=cfg.min_turn_gap,
            phrases=cfg.phrases,
            suppress_when_any_of=cfg.suppress_when_any_of,
            rng=rng,
            enabled=cfg.enabled,
        )

    # -------------------- core surface --------------------

    def should_inject(
        self,
        turn_number: int,
        ctx_flags: Optional[dict] = None,
    ) -> tuple[bool, str]:
        """Return (ok, reason). `reason` explains a skip when ok=False."""
        if not self.enabled:
            return False, "disabled"
        if not self.phrases:
            return False, "no_phrases"
        if self._last_fire_turn is not None:
            if (turn_number - self._last_fire_turn) < self.min_turn_gap:
                return False, "min_turn_gap"
        flags = ctx_flags or {}
        active = [k for k in self.suppress_flags if flags.get(k)]
        if active:
            return False, f"suppressed:{','.join(sorted(active))}"
        roll = self._rng.randint(1, self.rate_denominator)
        if roll != 1:
            return False, "roll"
        return True, "roll"

    def pick_phrase(self) -> str:
        if not self.phrases:
            return ""
        return self._rng.choice(self.phrases)

    def maybe_inject(
        self,
        response_text: str,
        turn_number: int,
        ctx_flags: Optional[dict] = None,
    ) -> AnchorResult:
        ok, reason = self.should_inject(turn_number, ctx_flags)
        if not ok:
            return AnchorResult(text=response_text, injected=False, reason=reason)
        phrase = self.pick_phrase()
        if not phrase:
            return AnchorResult(text=response_text, injected=False, reason="no_phrase")
        # Avoid double-anchoring: if the text already carries an anchor phrase
        # substring, don't pile another on.
        lower = response_text.lower()
        if any(p.lower()[:20] in lower for p in self.phrases if p):
            return AnchorResult(text=response_text, injected=False, reason="already_present")
        stitched = self._stitch(response_text, phrase)
        self._last_fire_turn = turn_number
        return AnchorResult(text=stitched, injected=True, phrase=phrase, reason=reason)

    # -------------------- internal --------------------

    def _stitch(self, response_text: str, phrase: str) -> str:
        """Attach the anchor as a natural trailing thought."""
        base = (response_text or "").rstrip()
        if not base:
            return phrase
        # If the last char already closes a sentence, just append.
        if base.endswith((".", "!", "?", "…")):
            return f"{base} {phrase}"
        return f"{base}. {phrase}"
