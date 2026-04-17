"""
SafetyLayer facade — composes the four sub-layers.

PersonaCore holds one SafetyLayer instance. It exposes the hooks the
persona pipeline needs:

  - `pre_llm(text)` -> (scrubbed_text, mapping)
  - `unscrub(text, mapping)`
  - `maybe_anchor(response_text, turn_number, flags)` -> AnchorResult
  - `record_turn_duration(ms)`
  - `check_flags()`

Turn context for anchor suppression is optional; when absent we use the
default suppress list (disagreement/correction/hard_truth/user_distress/
vulnerable_admission). Same shape as the dict the orchestrator already
builds for latency planning.
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

from .config import SafetyConfig, load_safety_config
from .health_monitor import HealthFlag, HealthMonitor
from .pii_scrubber import PIIScrubber, ScrubResult
from .reality_anchors import AnchorResult, RealityAnchorInjector


class SafetyLayer:
    def __init__(
        self,
        cfg: SafetyConfig,
        state_dir: str | Path,
        *,
        rng: Optional[random.Random] = None,
    ):
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.anchors = RealityAnchorInjector.from_config(
            cfg.reality_anchors, rng=rng or random.Random()
        )
        self.health = HealthMonitor.from_config(
            self.state_dir / "health.db", cfg.health_monitor
        )
        self.pii = PIIScrubber.from_config(cfg.pii_scrubber)
        self._turn_counter = 0

    @classmethod
    def from_config(
        cls,
        config_path: str | Path,
        state_dir: str | Path,
        *,
        rng: Optional[random.Random] = None,
    ) -> "SafetyLayer":
        return cls(load_safety_config(config_path), state_dir, rng=rng)

    # -------------------- LLM bracketing --------------------

    def pre_llm(self, text: str) -> ScrubResult:
        return self.pii.scrub(text)

    def unscrub(self, text: str, mapping: dict[str, str]) -> str:
        return self.pii.unscrub(text, mapping)

    # -------------------- anchor --------------------

    def maybe_anchor(
        self,
        response_text: str,
        ctx_flags: Optional[dict] = None,
    ) -> AnchorResult:
        self._turn_counter += 1
        return self.anchors.maybe_inject(response_text, self._turn_counter, ctx_flags)

    # -------------------- health --------------------

    def record_turn_duration(self, duration_ms: float) -> None:
        self.health.record_turn(duration_ms)

    def check_flags(self) -> list[HealthFlag]:
        return self.health.check_flags()
