"""Load persona YAML configs (Renée / Aiden) into validated dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PersonaDef:
    name: str
    raw: dict = field(default_factory=dict)

    @property
    def identity(self) -> dict:
        return self.raw.get("identity", {})

    @property
    def personality(self) -> dict:
        return self.raw.get("personality", {})

    @property
    def baseline_mood(self) -> dict:
        return self.raw.get("baseline_mood", {})

    @property
    def circadian(self) -> dict[int, float]:
        return {int(k): float(v) for k, v in self.raw.get("circadian", {}).items()}

    @property
    def opinions(self) -> dict:
        return self.raw.get("opinions", {})

    @property
    def speech_patterns(self) -> dict:
        return self.raw.get("speech_patterns", {})

    @property
    def quirks(self) -> list[str]:
        return self.raw.get("quirks", [])

    @property
    def relationship_context(self) -> dict:
        return self.raw.get("relationship_context", {})

    @property
    def hard_rules(self) -> list[str]:
        return self.raw.get("hard_rules", [])

    @property
    def hedge_frequency(self) -> float:
        return float(self.speech_patterns.get("hedge_frequency", 0.3))

    @property
    def never_uses(self) -> list[str]:
        return list(self.speech_patterns.get("never_uses", []))


def load_persona(path: str | Path) -> PersonaDef:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = raw.get("identity", {}).get("name", path.stem)
    return PersonaDef(name=name, raw=raw)
