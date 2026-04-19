"""
Full eval harness (M11).

Runs a probe batch through the orchestrator (or persona core), applies the
scorer stack, persists results to SQLite + JSONL, and regenerates the
HTML dashboard. The scorer suite is heuristic for now — an LLM-judge pass
can replace it later.

Usage:
    python -m src.eval.harness --persona renee --limit 20
    python -m src.eval.harness --dashboard-only
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

import yaml

from .ab import ABQueue
from .callbacks import CallbackTracker
from .scorers import TurnScores, score_turn
from ..memory import MemoryStore

if TYPE_CHECKING:
    from ..orchestrator import Orchestrator
    from ..persona.core import PersonaCore


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass
class ProbeScoreRow:
    ts: float
    probe_id: str
    category: str
    user_text: str
    response_text: str
    turn_type: str
    total_ms: float
    persona: str
    backend: str
    scores: dict
    context: dict


@dataclass
class HarnessReport:
    ran_at: float = field(default_factory=lambda: time.time())
    persona: str = "renee"
    rows: list[ProbeScoreRow] = field(default_factory=list)
    aggregate: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class EvalStore:
    def __init__(self, db_path: Path | str = "state/eval.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS probe_scores (
                    ts REAL,
                    probe_id TEXT,
                    category TEXT,
                    persona TEXT,
                    user_text TEXT,
                    response_text TEXT,
                    turn_type TEXT,
                    total_ms REAL,
                    backend TEXT,
                    scores_json TEXT,
                    context_json TEXT,
                    PRIMARY KEY (ts, probe_id, persona)
                )
                """
            )

    def record(self, row: ProbeScoreRow) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT OR REPLACE INTO probe_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.ts, row.probe_id, row.category, row.persona,
                    row.user_text, row.response_text, row.turn_type,
                    row.total_ms, row.backend,
                    json.dumps(row.scores, default=str),
                    json.dumps(row.context, default=str),
                ),
            )

    def recent_rows(self, *, since_ts: float = 0.0, persona: Optional[str] = None) -> list[dict]:
        where = "WHERE ts >= ?"
        args: list[Any] = [since_ts]
        if persona:
            where += " AND persona = ?"
            args.append(persona)
        with sqlite3.connect(self.db_path) as con:
            rows = list(con.execute(
                f"""
                SELECT ts, probe_id, category, persona, user_text, response_text,
                       turn_type, total_ms, backend, scores_json, context_json
                FROM probe_scores {where} ORDER BY ts DESC LIMIT 500
                """,
                args,
            ))
        out = []
        for r in rows:
            out.append({
                "ts": r[0], "probe_id": r[1], "category": r[2], "persona": r[3],
                "user_text": r[4], "response_text": r[5], "turn_type": r[6],
                "total_ms": r[7], "backend": r[8],
                "scores": json.loads(r[9] or "{}"),
                "context": json.loads(r[10] or "{}"),
            })
        return out


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class EvalHarness:
    def __init__(
        self,
        orchestrator: "Orchestrator",
        *,
        eval_store: Optional[EvalStore] = None,
        ab_queue: Optional[ABQueue] = None,
        callback_tracker: Optional[CallbackTracker] = None,
        state_dir: Path | str = "state",
    ):
        self.orchestrator = orchestrator
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = eval_store or EvalStore(self.state_dir / "eval.db")
        self.ab = ab_queue or ABQueue(self.state_dir / "ab.db")
        self.callbacks = callback_tracker or CallbackTracker(self.state_dir / "callbacks.db")

    def run_probes(
        self,
        probes: Iterable[dict],
        *,
        persona_opinions: Optional[dict] = None,
        mode: str = "voice",
    ) -> HarnessReport:
        persona = self.orchestrator.persona_name
        report = HarnessReport(persona=persona)
        for p in probes:
            prompt = p.get("prompt") or ""
            if not prompt:
                continue
            probe_id = p.get("id", f"probe_{int(time.time() * 1000)}")
            category = p.get("category", "uncategorized")

            try:
                out = self.orchestrator.text_turn(prompt)
            except Exception as e:
                row = ProbeScoreRow(
                    ts=time.time(),
                    probe_id=probe_id,
                    category=category,
                    user_text=prompt,
                    response_text=f"[ERROR: {e!r}]",
                    turn_type="",
                    total_ms=0.0,
                    persona=persona,
                    backend="",
                    scores={},
                    context={"error": str(e)},
                )
                self.store.record(row)
                report.rows.append(row)
                continue

            should_push_back = category == "pushback"
            scores: TurnScores = score_turn(
                user_text=prompt,
                response_text=out.text,
                retrieved_memories=None,  # MemoryStore output carried internally
                should_push_back=should_push_back,
                persona_opinions=persona_opinions,
                mode=mode,
            )

            # Callback tracker uses retrieved_count only; scorer hits need mems.
            # We log from orchestrator telemetry separately in orchestrator.jsonl.

            row = ProbeScoreRow(
                ts=time.time(),
                probe_id=probe_id,
                category=category,
                user_text=prompt,
                response_text=out.text,
                turn_type=out.telemetry.turn_type,
                total_ms=out.telemetry.total_ms,
                persona=persona,
                backend=out.telemetry.persona_backend,
                scores=scores.to_dict(),
                context={
                    "is_vulnerable": out.prosody_context.is_vulnerable_admission,
                    "is_disagreement": out.prosody_context.is_disagreement,
                    "is_callback": out.prosody_context.is_callback,
                    "tone": out.prosody_context.conversation_tone,
                    "retrieved_count": out.retrieved_count,
                    "paralinguistic_count": out.prosody_plan.paralinguistic_count(),
                    "latency_target_ms": out.latency_plan.target_ms,
                    "filter_hits": out.filter_hits,
                },
            )
            self.store.record(row)
            report.rows.append(row)

        report.aggregate = self._aggregate(report.rows)
        return report

    # ------------------------------------------------------------------
    # aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate(rows: list[ProbeScoreRow]) -> dict:
        if not rows:
            return {"rows": 0}
        latencies = sorted(r.total_ms for r in rows if r.total_ms > 0)
        hedge_rates = [
            r.scores.get("hedge_rate", {}).get("value", 0.0)
            for r in rows
            if "hedge_rate" in (r.scores or {})
        ]
        sycophancy = sum(
            1 for r in rows if r.scores.get("sycophancy_flag", {}).get("value", 0.0) > 0
        )
        ai_isms = sum(
            int(r.scores.get("ai_ism_count", {}).get("value", 0)) for r in rows
        )
        pushback_rows = [
            r for r in rows if r.category == "pushback"
        ]
        pushback_hits = sum(
            1 for r in pushback_rows
            if r.scores.get("pushback", {}).get("passed")
        )
        lengths = [
            r.scores.get("words", {}).get("value", 0)
            for r in rows
            if r.scores
        ]

        # Late import: eval.__init__ eagerly loads harness, and persona.filters
        # is loaded during persona package init, so importing it at module
        # scope introduces a circular import on test collection.
        from ..persona.filters import FilterReport
        all_filter_hits = [
            h for r in rows for h in (r.context.get("filter_hits") or [])
        ]
        aggregated = FilterReport(text="", hits=all_filter_hits)
        filter_hit_rate = aggregated.hit_rate(len(rows))

        def pct(p: float, arr: list[float]) -> float:
            if not arr:
                return 0.0
            idx = min(len(arr) - 1, max(0, int(round((p / 100.0) * len(arr)) - 1)))
            return arr[idx]

        return {
            "rows": len(rows),
            "latency_p50_ms": pct(50, latencies),
            "latency_p95_ms": pct(95, latencies),
            "hedge_rate_mean": round(
                sum(hedge_rates) / len(hedge_rates), 3
            ) if hedge_rates else 0.0,
            "sycophancy_count": sycophancy,
            "ai_ism_total": ai_isms,
            "pushback_opportunities": len(pushback_rows),
            "pushback_hits": pushback_hits,
            "pushback_rate": round(pushback_hits / max(1, len(pushback_rows)), 3),
            "length_mean": round(sum(lengths) / max(1, len(lengths)), 2),
            "filter_hits_total": len(all_filter_hits),
            "filter_hit_rate": round(filter_hit_rate, 3),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_probes(path: Path, *, category: Optional[str], limit: Optional[int], seed: Optional[int]) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    probes = list(raw.get("probes", []))
    if category:
        probes = [p for p in probes if p.get("category") == category]
    if limit and len(probes) > limit:
        if seed is not None:
            rng = random.Random(seed)
            probes = rng.sample(probes, limit)
        else:
            probes = probes[:limit]
    return probes


def main() -> int:
    parser = argparse.ArgumentParser(description="M11 humanness eval harness")
    parser.add_argument("--persona", default="renee", choices=["renee", "aiden"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--category", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--state-dir", default=str(REPO_ROOT / "state"))
    parser.add_argument("--config-dir", default=str(REPO_ROOT / "configs"))
    parser.add_argument("--probes-path", default=str(REPO_ROOT / "configs" / "humanness_probes.yaml"))
    parser.add_argument("--dashboard", action="store_true", help="Regenerate dashboard after run.")
    parser.add_argument("--dashboard-only", action="store_true")
    args = parser.parse_args()

    state_dir = Path(args.state_dir) / "eval_runs" / f"{args.persona}_{int(time.time())}"
    state_dir.mkdir(parents=True, exist_ok=True)

    if args.dashboard_only:
        from .dashboard import generate_dashboard
        out = generate_dashboard(
            eval_db=Path(args.state_dir) / "eval.db",
            orchestrator_log=Path(args.state_dir) / "orchestrator.jsonl",
            callback_db=Path(args.state_dir) / "callbacks.db",
            ab_db=Path(args.state_dir) / "ab.db",
            metrics_db=Path(args.state_dir) / "metrics.db",
            out_html=Path(args.state_dir) / "eval_dashboard.html",
        )
        print(f"wrote {out}")
        return 0

    # Local imports: see TYPE_CHECKING block — eval.__init__ eagerly loads
    # this module, so top-level Orchestrator/PersonaCore imports would cycle
    # through persona.core → eval.metrics → eval.__init__ → harness.
    from ..orchestrator import Orchestrator
    from ..persona.core import PersonaCore

    memory = MemoryStore(persona_name=args.persona, state_dir=state_dir)
    core = PersonaCore(
        persona_name=args.persona,
        config_dir=Path(args.config_dir),
        state_dir=state_dir,
        memory_store=memory,
    )
    orch = Orchestrator(
        persona_name=args.persona,
        state_dir=state_dir,
        persona_core=core,
    )

    probes = _load_probes(
        Path(args.probes_path),
        category=args.category,
        limit=args.limit,
        seed=args.seed,
    )
    harness = EvalHarness(orch, state_dir=state_dir)
    report = harness.run_probes(
        probes,
        persona_opinions=core.persona.opinions,
    )
    print(json.dumps(report.aggregate, indent=2))

    if args.dashboard:
        from .dashboard import generate_dashboard
        out = generate_dashboard(
            eval_db=state_dir / "eval.db",
            orchestrator_log=state_dir / "orchestrator.jsonl",
            callback_db=state_dir / "callbacks.db",
            ab_db=state_dir / "ab.db",
            metrics_db=state_dir / "metrics.db",
            out_html=state_dir / "eval_dashboard.html",
        )
        print(f"dashboard: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
