"""
Mood state — six-axis vector, drifts toward baseline, influenced by circadian
rhythm and conversational tone.

Persisted per-persona in SQLite at state/<persona>_mood.db.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, asdict, replace
from datetime import datetime
from pathlib import Path

from .persona_def import PersonaDef

AXES = ("energy", "warmth", "playfulness", "focus", "patience", "curiosity")


@dataclass
class MoodState:
    energy: float = 0.65
    warmth: float = 0.75
    playfulness: float = 0.70
    focus: float = 0.75
    patience: float = 0.65
    curiosity: float = 0.80
    last_updated: float = 0.0

    def clamped(self) -> "MoodState":
        return MoodState(
            energy=max(0.0, min(1.0, self.energy)),
            warmth=max(0.0, min(1.0, self.warmth)),
            playfulness=max(0.0, min(1.0, self.playfulness)),
            focus=max(0.0, min(1.0, self.focus)),
            patience=max(0.0, min(1.0, self.patience)),
            curiosity=max(0.0, min(1.0, self.curiosity)),
            last_updated=self.last_updated,
        )

    def summary(self) -> str:
        parts = []
        if self.energy < 0.4:
            parts.append("low energy, a little tired")
        elif self.energy > 0.8:
            parts.append("energetic")
        if self.warmth < 0.4:
            parts.append("a bit distant")
        elif self.warmth > 0.8:
            parts.append("warm, close")
        if self.playfulness > 0.75:
            parts.append("playful")
        elif self.playfulness < 0.35:
            parts.append("not in a playful mood")
        if self.focus < 0.4:
            parts.append("a touch scattered")
        elif self.focus > 0.85:
            parts.append("sharp, focused")
        if self.patience < 0.35:
            parts.append("running thin on patience")
        elif self.patience > 0.85:
            parts.append("patient")
        if self.curiosity > 0.85:
            parts.append("curious, lit up by ideas")
        elif self.curiosity < 0.35:
            parts.append("flat, not curious today")
        return ", ".join(parts) or "level, neutral"


def _baseline_from_persona(persona: PersonaDef) -> MoodState:
    bm = persona.baseline_mood or {}
    return MoodState(
        energy=float(bm.get("energy", 0.65)),
        warmth=float(bm.get("warmth", 0.75)),
        playfulness=float(bm.get("playfulness", 0.70)),
        focus=float(bm.get("focus", 0.75)),
        patience=float(bm.get("patience", 0.65)),
        curiosity=float(bm.get("curiosity", 0.80)),
        last_updated=time.time(),
    )


def _circadian_energy_multiplier(persona: PersonaDef, now: datetime | None = None) -> float:
    now = now or datetime.now()
    table = persona.circadian
    if not table:
        return 1.0
    hours = sorted(table.keys())
    h = now.hour + now.minute / 60.0
    lower = max([x for x in hours if x <= h], default=hours[0])
    upper = min([x for x in hours if x >= h], default=hours[-1])
    if lower == upper:
        return table[lower]
    span = (upper - lower) or 1
    t = (h - lower) / span
    return (1 - t) * table[lower] + t * table[upper]


class MoodStore:
    """SQLite-backed mood persistence."""

    def __init__(self, persona: PersonaDef, state_dir: Path):
        self.persona = persona
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        db_name = f"{persona.name.lower()}_mood.db"
        self.db_path = self.state_dir / db_name
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS mood (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    energy REAL, warmth REAL, playfulness REAL,
                    focus REAL, patience REAL, curiosity REAL,
                    last_updated REAL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS mood_log (
                    ts REAL PRIMARY KEY,
                    event TEXT,
                    delta_json TEXT,
                    state_json TEXT
                )
                """
            )
            # Bad-day state: random ~1 in 15 days a low-mood floor clamps down
            # for a few hours. Reason is not surfaced to Renée, she's just off.
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS bad_day (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    active_until REAL,
                    started_at REAL
                )
                """
            )

    def load(self) -> MoodState:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT energy,warmth,playfulness,focus,patience,curiosity,last_updated FROM mood WHERE id=1"
            ).fetchone()
        if row is None:
            state = _baseline_from_persona(self.persona)
            self.save(state, event="init", delta={})
            return state
        return MoodState(*row)

    def save(self, state: MoodState, event: str = "update", delta: dict | None = None):
        state = state.clamped()
        state.last_updated = time.time()
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT INTO mood (id,energy,warmth,playfulness,focus,patience,curiosity,last_updated)
                VALUES (1,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  energy=excluded.energy, warmth=excluded.warmth, playfulness=excluded.playfulness,
                  focus=excluded.focus, patience=excluded.patience, curiosity=excluded.curiosity,
                  last_updated=excluded.last_updated
                """,
                (state.energy, state.warmth, state.playfulness, state.focus,
                 state.patience, state.curiosity, state.last_updated),
            )
            con.execute(
                "INSERT INTO mood_log (ts,event,delta_json,state_json) VALUES (?,?,?,?)",
                (state.last_updated, event, json.dumps(delta or {}), json.dumps(asdict(state))),
            )

    def apply_drift(self, state: MoodState, drift_rate_per_hour: float = 0.04) -> MoodState:
        """Pull current state toward baseline, and recompute circadian energy floor/ceiling.

        Called whenever we load mood, so that time-based drift shows up naturally.
        """
        now = time.time()
        baseline = _baseline_from_persona(self.persona)
        hours = max(0.0, (now - state.last_updated) / 3600.0) if state.last_updated else 0.0
        if hours == 0:
            drifted = state
        else:
            factor = 1 - math.exp(-drift_rate_per_hour * hours)
            drifted = MoodState(
                energy=state.energy + (baseline.energy - state.energy) * factor,
                warmth=state.warmth + (baseline.warmth - state.warmth) * factor,
                playfulness=state.playfulness + (baseline.playfulness - state.playfulness) * factor,
                focus=state.focus + (baseline.focus - state.focus) * factor,
                patience=state.patience + (baseline.patience - state.patience) * factor,
                curiosity=state.curiosity + (baseline.curiosity - state.curiosity) * factor,
                last_updated=now,
            )
        # apply circadian energy envelope: multiply baseline energy by circadian factor,
        # then pull current energy toward that point softly
        circ = _circadian_energy_multiplier(self.persona, datetime.now())
        target_energy = max(0.0, min(1.0, baseline.energy * circ))
        drifted.energy = drifted.energy + (target_energy - drifted.energy) * 0.25
        return drifted.clamped()

    def load_with_drift(self) -> MoodState:
        state = self.load()
        drifted = self.apply_drift(state)
        drifted = self._maybe_bad_day(drifted)
        if drifted != state:
            self.save(drifted, event="drift", delta={"cause": "circadian+baseline pull"})
        return drifted

    def _maybe_bad_day(self, state: MoodState) -> MoodState:
        """Apply a random bad-day floor if one is active, or roll to start one.

        When Renée has a bad day, her warmth, playfulness, and patience are
        clamped below 0.45 for 3-6 hours. She doesn't know why. PJ doesn't
        either. It's a day she's just off. Rolls ~1 in 15 days.
        """
        import random as _random
        now = time.time()
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT active_until FROM bad_day WHERE id=1").fetchone()
        active_until = float(row[0]) if row else 0.0

        if active_until and now < active_until:
            # bad day in progress, clamp mood
            return MoodState(
                energy=min(state.energy, 0.55),
                warmth=min(state.warmth, 0.45),
                playfulness=min(state.playfulness, 0.35),
                focus=state.focus,
                patience=min(state.patience, 0.45),
                curiosity=min(state.curiosity, 0.55),
                last_updated=state.last_updated,
            )

        # Roll for a new bad day. We roll at most once per real day (24h since
        # last roll) to keep probability sane across rapid mood loads.
        last_roll_path = self.state_dir / f".{self.persona.name.lower()}_bad_day_last_roll"
        try:
            last_roll = float(last_roll_path.read_text().strip()) if last_roll_path.exists() else 0.0
        except Exception:
            last_roll = 0.0
        if now - last_roll < 86400:
            return state
        try:
            last_roll_path.write_text(str(now))
        except Exception:
            pass
        # 1 in 15 chance
        if _random.random() < (1.0 / 15.0):
            duration = _random.uniform(3, 6) * 3600.0
            with sqlite3.connect(self.db_path) as con:
                con.execute(
                    "INSERT OR REPLACE INTO bad_day (id, active_until, started_at) VALUES (1, ?, ?)",
                    (now + duration, now),
                )
            return self._maybe_bad_day(state)  # re-enter to clamp
        return state

    def bad_day_active(self) -> bool:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT active_until FROM bad_day WHERE id=1").fetchone()
        if not row:
            return False
        return time.time() < float(row[0] or 0.0)

    def apply_tone(self, state: MoodState, user_tone: dict) -> MoodState:
        """Update mood based on the inferred tone of the last exchange.

        user_tone keys: valence (-1..1), intensity (0..1), disagreement (0..1), warmth (0..1)
        """
        valence = float(user_tone.get("valence", 0.0))
        intensity = float(user_tone.get("intensity", 0.3))
        disagreement = float(user_tone.get("disagreement", 0.0))
        user_warmth = float(user_tone.get("warmth", 0.5))

        delta_energy = 0.04 * valence * intensity
        delta_warmth = 0.05 * (user_warmth - 0.5) * 2 * intensity
        delta_playfulness = 0.04 * max(0, valence) * intensity - 0.02 * disagreement
        delta_patience = -0.08 * disagreement * intensity + 0.01 * max(0, valence) * intensity
        delta_curiosity = 0.02 * intensity
        delta_focus = 0.02 * intensity - 0.01 * disagreement

        new_state = MoodState(
            energy=state.energy + delta_energy,
            warmth=state.warmth + delta_warmth,
            playfulness=state.playfulness + delta_playfulness,
            focus=state.focus + delta_focus,
            patience=state.patience + delta_patience,
            curiosity=state.curiosity + delta_curiosity,
            last_updated=time.time(),
        ).clamped()
        delta = {
            "energy": delta_energy, "warmth": delta_warmth, "playfulness": delta_playfulness,
            "focus": delta_focus, "patience": delta_patience, "curiosity": delta_curiosity,
            "user_tone": user_tone,
        }
        self.save(new_state, event="tone", delta=delta)
        return new_state
