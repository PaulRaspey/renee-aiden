"""YAML-safe write operations for the Tuning tab.

All writes load the current YAML, apply a minimal targeted update, and
re-dump the file. The old_value is returned so the audit log can capture
a full before/after pair for every change.

`apply_persona_tuning` and `apply_safety_tuning` also attempt a runtime
reload on the orchestrator's persona core or safety layer so PJ sees the
change on the next turn without restarting the pod.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


MOOD_AXES = ("energy", "warmth", "playfulness", "focus", "patience", "curiosity")


@dataclass
class TuningResult:
    field: str
    old_value: Any
    new_value: Any
    reload_attempted: bool
    reload_ok: bool


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def dump_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def update_mood_baseline(
    *,
    persona_yaml: Path,
    axis: str,
    value: float,
    orchestrator: Any = None,
) -> TuningResult:
    if axis not in MOOD_AXES:
        raise ValueError(f"unknown mood axis: {axis}")
    data = load_yaml(persona_yaml)
    baseline = dict(data.get("baseline_mood") or {})
    old_value = float(baseline.get(axis, 0.5))
    baseline[axis] = float(_clamp(value, 0.0, 1.0))
    data["baseline_mood"] = baseline
    dump_yaml(persona_yaml, data)
    reload_ok = _reload_persona(orchestrator)
    return TuningResult(
        field=f"persona.baseline_mood.{axis}",
        old_value=old_value,
        new_value=baseline[axis],
        reload_attempted=orchestrator is not None,
        reload_ok=reload_ok,
    )


def update_hedge_frequency(
    *,
    persona_yaml: Path,
    value: float,
    orchestrator: Any = None,
) -> TuningResult:
    data = load_yaml(persona_yaml)
    speech = dict(data.get("speech_patterns") or {})
    old_value = float(speech.get("hedge_frequency", 0.3))
    speech["hedge_frequency"] = float(_clamp(value, 0.0, 1.0))
    data["speech_patterns"] = speech
    dump_yaml(persona_yaml, data)
    reload_ok = _reload_persona(orchestrator)
    return TuningResult(
        field="persona.speech_patterns.hedge_frequency",
        old_value=old_value,
        new_value=speech["hedge_frequency"],
        reload_attempted=orchestrator is not None,
        reload_ok=reload_ok,
    )


def update_never_uses(
    *,
    persona_yaml: Path,
    phrases: list[str],
    orchestrator: Any = None,
) -> TuningResult:
    data = load_yaml(persona_yaml)
    speech = dict(data.get("speech_patterns") or {})
    old_value = list(speech.get("never_uses") or [])
    # dedupe while preserving order; strip falsy entries
    cleaned = []
    seen = set()
    for p in phrases:
        s = str(p or "").strip()
        if s and s not in seen:
            cleaned.append(s)
            seen.add(s)
    speech["never_uses"] = cleaned
    data["speech_patterns"] = speech
    dump_yaml(persona_yaml, data)
    reload_ok = _reload_persona(orchestrator)
    return TuningResult(
        field="persona.speech_patterns.never_uses",
        old_value=old_value,
        new_value=cleaned,
        reload_attempted=orchestrator is not None,
        reload_ok=reload_ok,
    )


def update_circadian(
    *,
    persona_yaml: Path,
    table: dict[int, float],
    orchestrator: Any = None,
) -> TuningResult:
    data = load_yaml(persona_yaml)
    old_value = dict(data.get("circadian") or {})
    cleaned = {}
    for hour, mult in table.items():
        h = int(hour)
        if not 0 <= h <= 23:
            raise ValueError(f"hour out of range: {h}")
        cleaned[h] = float(_clamp(mult, 0.0, 2.0))
    data["circadian"] = cleaned
    dump_yaml(persona_yaml, data)
    reload_ok = _reload_persona(orchestrator)
    return TuningResult(
        field="persona.circadian",
        old_value=old_value,
        new_value=cleaned,
        reload_attempted=orchestrator is not None,
        reload_ok=reload_ok,
    )


def update_safety_caps(
    *,
    safety_yaml: Path,
    daily_cap_minutes: Optional[int] = None,
    reality_anchor_rate_denominator: Optional[int] = None,
    bad_day_probability_per_day: Optional[float] = None,
    safety_layer: Any = None,
) -> TuningResult:
    """Update one or more sensitive safety values. Only the fields passed in
    as non-None are written; others stay as-is."""
    data = load_yaml(safety_yaml)
    old = {
        "daily_cap_minutes": int(
            (data.get("health_monitor") or {}).get("daily_cap_minutes", 120)
        ),
        "reality_anchor_rate_denominator": int(
            (data.get("reality_anchors") or {}).get("rate_denominator", 50)
        ),
        "bad_day_probability_per_day": float(
            (data.get("bad_day") or {}).get("probability_per_day", 1.0 / 15.0)
        ),
    }
    new = dict(old)
    if daily_cap_minutes is not None:
        hm = dict(data.get("health_monitor") or {})
        hm["daily_cap_minutes"] = int(max(0, int(daily_cap_minutes)))
        data["health_monitor"] = hm
        new["daily_cap_minutes"] = hm["daily_cap_minutes"]
    if reality_anchor_rate_denominator is not None:
        ra = dict(data.get("reality_anchors") or {})
        ra["rate_denominator"] = max(1, int(reality_anchor_rate_denominator))
        data["reality_anchors"] = ra
        new["reality_anchor_rate_denominator"] = ra["rate_denominator"]
    if bad_day_probability_per_day is not None:
        bd = dict(data.get("bad_day") or {})
        bd["probability_per_day"] = float(
            _clamp(float(bad_day_probability_per_day), 0.0, 1.0)
        )
        data["bad_day"] = bd
        new["bad_day_probability_per_day"] = bd["probability_per_day"]
    dump_yaml(safety_yaml, data)
    reload_ok = _reload_safety(safety_layer)
    return TuningResult(
        field="safety.caps",
        old_value=old,
        new_value=new,
        reload_attempted=safety_layer is not None,
        reload_ok=reload_ok,
    )


def update_voice_params(
    *,
    voice_yaml: Path,
    stability: Optional[float] = None,
    similarity_boost: Optional[float] = None,
    style: Optional[float] = None,
) -> TuningResult:
    data = load_yaml(voice_yaml)
    old = {
        "stability": float(data.get("stability", 0.3)),
        "similarity_boost": float(data.get("similarity_boost", 0.89)),
        "style": float(data.get("style", 0.0)),
    }
    new = dict(old)
    if stability is not None:
        data["stability"] = float(_clamp(stability, 0.0, 1.0))
        new["stability"] = data["stability"]
    if similarity_boost is not None:
        data["similarity_boost"] = float(_clamp(similarity_boost, 0.0, 1.0))
        new["similarity_boost"] = data["similarity_boost"]
    if style is not None:
        data["style"] = float(_clamp(style, 0.0, 1.0))
        new["style"] = data["style"]
    dump_yaml(voice_yaml, data)
    return TuningResult(
        field="voice.elevenlabs",
        old_value=old,
        new_value=new,
        reload_attempted=False,
        reload_ok=False,
    )


# -------------------- reload hooks --------------------


def _reload_persona(orchestrator: Any) -> bool:
    if orchestrator is None:
        return False
    core = getattr(orchestrator, "persona_core", None)
    if core is None:
        return False
    # PersonaCore stores its PersonaDef on .persona; reload from disk.
    try:
        from ..persona.persona_def import load_persona
        persona_yaml = Path(core.state_dir).parent / "configs" / f"{core.persona_name}.yaml"
        # Prefer explicit config_dir if the core tracks one.
        persona_yaml = getattr(core, "persona_yaml_path", None) or persona_yaml
        if not persona_yaml.exists():
            # fall back to the most common layout
            candidates = [
                Path("configs") / f"{core.persona_name}.yaml",
                Path("C:/Users/Epsar/Desktop/renee-aiden/configs") / f"{core.persona_name}.yaml",
            ]
            for c in candidates:
                if c.exists():
                    persona_yaml = c
                    break
        core.persona = load_persona(persona_yaml)
        # Filters cached hedge_min_ratio from persona.hedge_frequency; rebuild.
        from ..persona.filters import OutputFilters
        core.filters = OutputFilters(core.persona)
        return True
    except Exception:
        return False


def _reload_safety(safety_layer: Any) -> bool:
    if safety_layer is None:
        return False
    try:
        # Re-read the yaml into the in-memory SafetyConfig on the layer.
        from ..safety.config import load_safety_config
        cfg_path = Path(getattr(safety_layer, "config_path", "configs/safety.yaml"))
        if not cfg_path.exists():
            cfg_path = Path("configs/safety.yaml")
        safety_layer.cfg = load_safety_config(cfg_path)
        # Re-wire the anchor injector thresholds/phrases; keep the rng to
        # preserve the min-gap counter.
        from ..safety.reality_anchors import RealityAnchorInjector
        safety_layer.anchors = RealityAnchorInjector.from_config(
            safety_layer.cfg.reality_anchors,
            rng=safety_layer.anchors._rng,
        )
        # HealthMonitor reads its config at query time; just reset the cfg.
        safety_layer.health.cfg = safety_layer.cfg.health_monitor
        return True
    except Exception:
        return False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))
