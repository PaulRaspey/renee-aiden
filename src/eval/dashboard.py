"""
Simple HTML eval dashboard (M11).

Reads the eval SQLite + orchestrator JSONL + callback/AB stores and emits
a single self-contained HTML file. No JS frameworks, no auth — local-only.
Regenerated at the end of each probe run (or `--dashboard-only`).
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path
from statistics import mean, median
from typing import Optional


def _read_eval_rows(db: Path) -> list[dict]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        rows = list(con.execute(
            """
            SELECT ts, probe_id, category, persona, user_text, response_text,
                   turn_type, total_ms, backend, scores_json, context_json
            FROM probe_scores ORDER BY ts DESC LIMIT 200
            """
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


def _read_orchestrator_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    parsed: list[dict] = []
    for line in lines[-200:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def _read_callback_accuracy(db: Path) -> dict:
    if not db.exists():
        return {"opportunities": 0, "hits": 0, "accuracy": 0.0}
    with sqlite3.connect(db) as con:
        rows = list(con.execute("SELECT hit FROM callback_events"))
    if not rows:
        return {"opportunities": 0, "hits": 0, "accuracy": 0.0}
    hits = sum(1 for (h,) in rows if int(h) == 1)
    total = len(rows)
    return {"opportunities": total, "hits": hits, "accuracy": round(hits / total, 3)}


def _read_ab_winrate(db: Path) -> dict:
    if not db.exists():
        return {"ratings": 0, "candidate_wins": 0, "candidate_win_rate": 0.0}
    with sqlite3.connect(db) as con:
        rows = list(con.execute(
            """
            SELECT p.label_a, p.label_b, r.chosen
            FROM ab_pairs p JOIN ab_ratings r ON r.pair_id = p.pair_id
            """
        ))
    if not rows:
        return {"ratings": 0, "candidate_wins": 0, "candidate_win_rate": 0.0}
    cand_wins = 0
    for la, lb, chosen in rows:
        picked = la if chosen == "a" else lb
        if picked == "candidate":
            cand_wins += 1
    return {
        "ratings": len(rows),
        "candidate_wins": cand_wins,
        "candidate_win_rate": round(cand_wins / len(rows), 3),
    }


def _read_metrics_summary(db: Path) -> dict:
    if not db.exists():
        return {"turns": 0}
    with sqlite3.connect(db) as con:
        rows = list(con.execute("SELECT latency_ms FROM turn_metrics ORDER BY ts DESC LIMIT 200"))
    latencies = sorted(r[0] for r in rows if r[0])
    if not latencies:
        return {"turns": 0}

    def pct(p: float) -> float:
        idx = min(len(latencies) - 1, max(0, int(round((p / 100.0) * len(latencies)) - 1)))
        return round(latencies[idx], 1)

    return {
        "turns": len(latencies),
        "mean_ms": round(sum(latencies) / len(latencies), 1),
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
    }


def _aggregate_eval_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"rows": 0}
    hedge = [r["scores"].get("hedge_rate", {}).get("value", 0.0) for r in rows if r["scores"]]
    syc = sum(1 for r in rows if r["scores"].get("sycophancy_flag", {}).get("value", 0.0) > 0)
    ai = sum(int(r["scores"].get("ai_ism_count", {}).get("value", 0)) for r in rows)
    words = [r["scores"].get("words", {}).get("value", 0) for r in rows if r["scores"]]
    return {
        "rows": len(rows),
        "hedge_rate_mean": round(mean(hedge), 3) if hedge else 0,
        "sycophancy_count": syc,
        "ai_ism_total": ai,
        "words_mean": round(mean(words), 1) if words else 0,
        "words_median": int(median(words)) if words else 0,
    }


# ---------------------------------------------------------------------------
# renderer
# ---------------------------------------------------------------------------


_CSS = """
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 24px; color: #1a1a1a; background: #fafafa; }
  h1 { margin-bottom: 4px; }
  .sub { color: #666; margin-top: 0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 18px 0; }
  .card { background: white; border: 1px solid #e5e5e5; border-radius: 6px; padding: 12px 14px; }
  .card .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; }
  .card .value { font-size: 22px; font-weight: 600; margin-top: 2px; }
  table { border-collapse: collapse; width: 100%; background: white; font-size: 13px; }
  th, td { padding: 6px 8px; border-bottom: 1px solid #e5e5e5; text-align: left; vertical-align: top; }
  th { background: #f5f5f5; font-weight: 600; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .flag { color: #b91c1c; font-weight: 600; }
  .ok { color: #047857; }
  .pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; font-size: 12px; }
  .row-good { background: #f0fdf4; }
  .row-bad { background: #fef2f2; }
  details { margin: 6px 0; }
</style>
"""


def _card(label: str, value) -> str:
    return (
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(str(value))}</div></div>'
    )


def _escape_short(s: str, n: int = 120) -> str:
    s = (s or "").strip()
    if len(s) > n:
        s = s[:n - 1] + "…"
    return html.escape(s)


def generate_dashboard(
    *,
    eval_db: Path,
    orchestrator_log: Path,
    callback_db: Path,
    ab_db: Path,
    metrics_db: Optional[Path] = None,
    out_html: Path,
) -> Path:
    eval_rows = _read_eval_rows(eval_db)
    orch_log = _read_orchestrator_log(orchestrator_log)
    callback_stats = _read_callback_accuracy(callback_db)
    ab_stats = _read_ab_winrate(ab_db)
    metrics_stats = _read_metrics_summary(metrics_db) if metrics_db else {"turns": 0}
    agg = _aggregate_eval_rows(eval_rows)

    cards = [
        _card("probe rows", agg.get("rows", 0)),
        _card("hedge rate (mean)", agg.get("hedge_rate_mean", 0)),
        _card("sycophancy flags", agg.get("sycophancy_count", 0)),
        _card("AI-ism hits", agg.get("ai_ism_total", 0)),
        _card("words (median)", agg.get("words_median", 0)),
        _card("callback accuracy", callback_stats.get("accuracy", 0)),
        _card("A/B candidate win rate", ab_stats.get("candidate_win_rate", 0)),
        _card("metrics p50 ms", metrics_stats.get("p50_ms", "—")),
        _card("metrics p95 ms", metrics_stats.get("p95_ms", "—")),
    ]

    # Latency table from orchestrator.jsonl
    latency_rows = []
    for row in orch_log:
        t = row.get("telemetry", {})
        if not t:
            continue
        latency_rows.append({
            "ts": row.get("ts", 0),
            "persona": row.get("persona", ""),
            "turn_type": t.get("turn_type", ""),
            "persona_ms": t.get("persona_respond_ms", 0),
            "injector_ms": t.get("injector_plan_ms", 0),
            "prosody_ms": t.get("prosody_plan_ms", 0),
            "total_ms": t.get("total_ms", 0),
            "latency_target_ms": t.get("latency_plan_target_ms", 0),
        })

    latency_rows = latency_rows[-30:]

    probe_rows_html_parts = []
    for r in eval_rows[:60]:
        flags = []
        sc = r["scores"]
        if sc.get("sycophancy_flag", {}).get("value", 0) > 0:
            flags.append("sycophancy")
        if sc.get("ai_ism_count", {}).get("value", 0) > 0:
            flags.append("ai-ism")
        if sc.get("pushback", {}).get("passed") is False:
            flags.append("missed-pushback")
        row_class = "row-bad" if flags else ("row-good" if r.get("category") == "pushback" and sc.get("pushback", {}).get("passed") else "")
        flag_span = f'<span class="flag">{", ".join(flags)}</span>' if flags else '<span class="ok">ok</span>'
        probe_rows_html_parts.append(
            "<tr class='" + row_class + "'>"
            f"<td>{html.escape(r.get('probe_id', ''))}</td>"
            f"<td>{html.escape(r.get('category', ''))}</td>"
            f"<td>{_escape_short(r.get('user_text', ''), 80)}</td>"
            f"<td>{_escape_short(r.get('response_text', ''), 160)}</td>"
            f"<td class='num'>{html.escape(str(round(r.get('total_ms', 0), 1)))}</td>"
            f"<td>{flag_span}</td>"
            "</tr>"
        )

    latency_rows_html_parts = []
    for l in latency_rows:
        latency_rows_html_parts.append(
            "<tr>"
            f"<td>{html.escape(str(l['persona']))}</td>"
            f"<td>{html.escape(str(l['turn_type']))}</td>"
            f"<td class='num'>{html.escape(str(l['persona_ms']))}</td>"
            f"<td class='num'>{html.escape(str(l['injector_ms']))}</td>"
            f"<td class='num'>{html.escape(str(l['prosody_ms']))}</td>"
            f"<td class='num'>{html.escape(str(l['total_ms']))}</td>"
            f"<td class='num'>{html.escape(str(l['latency_target_ms']))}</td>"
            "</tr>"
        )

    body_parts = [
        "<h1>Renée eval dashboard</h1>",
        f'<p class="sub">regenerated from {html.escape(str(eval_db))} + siblings.</p>',
        '<div class="grid">' + "".join(cards) + "</div>",
        "<h2>Recent probe rows</h2>",
        "<table><thead><tr><th>probe</th><th>category</th><th>user</th><th>response</th><th>ms</th><th>flags</th></tr></thead><tbody>",
        "".join(probe_rows_html_parts) or "<tr><td colspan='6'>no probe runs yet</td></tr>",
        "</tbody></table>",
        "<h2>Latency — last 30 turns (orchestrator.jsonl)</h2>",
        "<table><thead><tr><th>persona</th><th>turn_type</th><th>persona ms</th><th>inj ms</th><th>prosody ms</th><th>total ms</th><th>target ms</th></tr></thead><tbody>",
        "".join(latency_rows_html_parts) or "<tr><td colspan='7'>no telemetry yet</td></tr>",
        "</tbody></table>",
    ]

    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(
        "<html><head><meta charset='utf-8'><title>Renée eval</title>"
        + _CSS
        + "</head><body>"
        + "".join(body_parts)
        + "</body></html>",
        encoding="utf-8",
    )
    return out_html
