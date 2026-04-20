"""Read-only snapshots of Renée's runtime state for the dashboard.

Everything here is pure reads; no orchestrator surgery, no writes to the
orchestrator. When a live orchestrator instance is absent (tests, cold
reads), the snapshot still returns a coherent dict by going straight to
the state dir.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ..persona.mood import AXES, MoodState, MoodStore
from ..persona.persona_def import PersonaDef, load_persona
from ..safety.config import load_safety_config
from ..safety.health_monitor import HealthMonitor


def live_snapshot(
    *,
    state_dir: Path,
    config_dir: Path,
    persona: str,
    orchestrator: Any = None,
    safety_layer: Any = None,
) -> dict:
    """Return the payload the Live tab polls every few seconds."""
    persona_def = load_persona(config_dir / f"{persona}.yaml")
    mood_store = MoodStore(persona_def, state_dir)
    current_mood = mood_store.load_with_drift()
    baseline = persona_def.baseline_mood or {}

    last_turns = _read_last_turns(state_dir, limit=10)
    latency = _latency_stats(state_dir, limit=200)
    anchor_stats = _anchor_stats(state_dir, limit=200)

    health_cfg = load_safety_config(config_dir / "safety.yaml").health_monitor
    health = HealthMonitor(state_dir / "health.db", cfg=health_cfg)
    daily_minutes = health.daily_minutes()
    seven_day = health.seven_day_average_minutes()
    thirty_day = health.thirty_day_average_minutes()
    bridge_allowed = health.bridge_allowed_now()
    cooldown_until = health.bridge_cooldown_until()

    return {
        "persona": persona,
        "ts": time.time(),
        "mood": {
            "current": _mood_axes(current_mood),
            "baseline": _baseline_axes(baseline),
            "bad_day": mood_store.bad_day_active(),
        },
        "bridge": {
            "allowed": bridge_allowed,
            "cooldown_until": cooldown_until,
            "farewell": health_cfg.cap_disconnect_message,
        },
        "last_turns": last_turns,
        "latency": latency,
        "anchor": anchor_stats,
        "health": {
            "daily_minutes": daily_minutes,
            "daily_cap_minutes": health_cfg.daily_cap_minutes,
            "seven_day_avg_minutes": seven_day,
            "thirty_day_avg_minutes": thirty_day,
        },
        "safety_layer_present": safety_layer is not None,
        "orchestrator_present": orchestrator is not None,
    }


def health_snapshot(
    *,
    state_dir: Path,
    config_dir: Path,
) -> dict:
    health_cfg = load_safety_config(config_dir / "safety.yaml").health_monitor
    health = HealthMonitor(state_dir / "health.db", cfg=health_cfg)
    rolling_30 = health.rolling_daily_minutes(30)
    sycophancy = _sycophancy_rate(state_dir, limit=500)
    return {
        "daily_minutes": health.daily_minutes(),
        "daily_cap_minutes": health_cfg.daily_cap_minutes,
        "seven_day_avg_minutes": health.seven_day_average_minutes(),
        "thirty_day_avg_minutes": health.thirty_day_average_minutes(),
        "rolling_30_day": [{"day": d, "minutes": m} for d, m in rolling_30],
        "sycophancy_rate": sycophancy,
        "bridge_allowed": health.bridge_allowed_now(),
        "bridge_cooldown_until": health.bridge_cooldown_until(),
        "latest_cooldown": health.latest_bridge_cooldown(),
    }


def logs_for_day(
    *,
    state_dir: Path,
    day_key: str,
) -> dict:
    path = state_dir / "logs" / "conversations" / f"{day_key}.log"
    if not path.exists():
        return {"day": day_key, "exists": False, "lines": []}
    lines = path.read_text(encoding="utf-8").splitlines()
    return {"day": day_key, "exists": True, "lines": lines}


def _mood_axes(mood: MoodState) -> list[dict]:
    return [{"axis": axis, "value": float(getattr(mood, axis))} for axis in AXES]


def _baseline_axes(baseline: dict) -> list[dict]:
    default = {
        "energy": 0.65, "warmth": 0.75, "playfulness": 0.70,
        "focus": 0.75, "patience": 0.65, "curiosity": 0.80,
    }
    return [
        {"axis": axis, "value": float(baseline.get(axis, default[axis]))}
        for axis in AXES
    ]


def _read_last_turns(state_dir: Path, *, limit: int = 10) -> list[dict]:
    path = state_dir / "orchestrator.jsonl"
    if not path.exists():
        return []
    # Read the last `limit` lines without loading the whole file.
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Budget a generous tail; the JSONL lines are small.
            tail = min(size, 512 * 1024)
            f.seek(size - tail)
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    lines = [ln for ln in data.splitlines() if ln.strip()]
    out: list[dict] = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _latency_stats(state_dir: Path, *, limit: int = 200) -> dict:
    db = state_dir / "metrics.db"
    if not db.exists():
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "last_backend": None}
    try:
        with sqlite3.connect(db) as c:
            rows = c.execute(
                "SELECT latency_ms, backend FROM turn_metrics ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except sqlite3.OperationalError:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "last_backend": None}
    if not rows:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "last_backend": None}
    lat = sorted(float(r[0] or 0.0) for r in rows)
    p50 = lat[len(lat) // 2]
    p95 = lat[max(0, int(len(lat) * 0.95) - 1)]
    last_backend = rows[0][1]  # rows are DESC ordered
    return {
        "count": len(rows),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "last_backend": last_backend,
    }


def _anchor_stats(state_dir: Path, *, limit: int = 200) -> dict:
    db = state_dir / "metrics.db"
    if not db.exists():
        return {"count": 0, "last_anchor_ts": None, "last_phrase": None, "rate": 0.0}
    try:
        with sqlite3.connect(db) as c:
            rows = c.execute(
                "SELECT ts, filter_hits FROM turn_metrics ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except sqlite3.OperationalError:
        return {"count": 0, "last_anchor_ts": None, "last_phrase": None, "rate": 0.0}
    anchor_count = 0
    last_ts = None
    last_phrase = None
    for ts, hits_json in rows:
        try:
            hits = json.loads(hits_json or "[]") or []
        except Exception:
            hits = []
        for h in hits:
            if isinstance(h, str) and h.startswith("anchor:"):
                anchor_count += 1
                if last_ts is None:
                    last_ts = float(ts)
                    last_phrase = h[len("anchor:"):]
    total = len(rows) or 1
    return {
        "count": anchor_count,
        "last_anchor_ts": last_ts,
        "last_phrase": last_phrase,
        "rate": round(anchor_count / total, 4),
    }


def _sycophancy_rate(state_dir: Path, *, limit: int = 500) -> float:
    db = state_dir / "metrics.db"
    if not db.exists():
        return 0.0
    try:
        with sqlite3.connect(db) as c:
            rows = c.execute(
                "SELECT sycophancy_flag FROM turn_metrics ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except sqlite3.OperationalError:
        return 0.0
    if not rows:
        return 0.0
    flagged = sum(1 for r in rows if int(r[0] or 0) == 1)
    return round(flagged / len(rows), 4)
