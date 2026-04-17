"""
Per-turn metrics: latency, backend, filter hits, token usage, retrieval size.

Every PersonaCore turn writes one row via `record_turn`. The evaluation
harness (M11 proper) reads this store for trend and regression detection.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class TurnMetric:
    ts: float
    persona: str
    backend: str
    model: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    filter_hits: list[str]
    regen: bool
    sycophancy_flag: bool
    retrieved_count: int
    user_chars: int
    response_chars: int
    mood_json: str
    receipt_id: str


class MetricsStore:
    def __init__(self, state_dir: Path | str = "state"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "metrics.db"
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS turn_metrics (
                    ts REAL PRIMARY KEY,
                    persona TEXT,
                    backend TEXT,
                    model TEXT,
                    latency_ms REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    filter_hits TEXT,
                    regen INTEGER,
                    sycophancy_flag INTEGER,
                    retrieved_count INTEGER,
                    user_chars INTEGER,
                    response_chars INTEGER,
                    mood_json TEXT,
                    receipt_id TEXT
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_metrics_persona ON turn_metrics(persona)")

    def record_turn(self, metric: TurnMetric):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO turn_metrics VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    metric.ts, metric.persona, metric.backend, metric.model,
                    metric.latency_ms, metric.input_tokens, metric.output_tokens,
                    json.dumps(metric.filter_hits), int(metric.regen),
                    int(metric.sycophancy_flag), metric.retrieved_count,
                    metric.user_chars, metric.response_chars, metric.mood_json,
                    metric.receipt_id,
                ),
            )

    def session_summary(self, persona: str | None = None, since_ts: float = 0.0) -> dict:
        where = "WHERE ts >= ?"
        args: list = [since_ts]
        if persona:
            where += " AND persona = ?"
            args.append(persona)
        with sqlite3.connect(self.db_path) as con:
            rows = list(con.execute(
                f"SELECT latency_ms, backend, filter_hits, sycophancy_flag, retrieved_count, input_tokens, output_tokens FROM turn_metrics {where}",
                args,
            ))
        if not rows:
            return {"turns": 0}
        latencies = sorted(r[0] for r in rows)
        total = len(latencies)
        backends: dict[str, int] = {}
        filter_hit_totals = 0
        sycophancy_hits = 0
        retrieved_sum = 0
        input_tok = 0
        output_tok = 0
        for lat, backend, hits_json, syc, ret, in_t, out_t in rows:
            backends[backend] = backends.get(backend, 0) + 1
            filter_hit_totals += len(json.loads(hits_json or "[]"))
            sycophancy_hits += int(syc)
            retrieved_sum += int(ret)
            input_tok += int(in_t or 0)
            output_tok += int(out_t or 0)

        def pct(p: float) -> float:
            idx = min(total - 1, max(0, int(round((p / 100.0) * total)) - 1))
            return latencies[idx]

        return {
            "turns": total,
            "latency_ms_p50": pct(50),
            "latency_ms_p95": pct(95),
            "latency_ms_p99": pct(99),
            "latency_ms_mean": sum(latencies) / total,
            "backends": backends,
            "filter_hits_total": filter_hit_totals,
            "filter_hits_per_turn": filter_hit_totals / total,
            "sycophancy_hits": sycophancy_hits,
            "retrieved_avg": retrieved_sum / total,
            "input_tokens_total": input_tok,
            "output_tokens_total": output_tok,
        }
