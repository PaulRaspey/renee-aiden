"""
Command-line report: read state/metrics.db, print session/trend summary.

Usage:
    python -m src.eval.report                   # default: renee, all time
    python -m src.eval.report --persona aiden
    python -m src.eval.report --since 24h       # last 24 hours
"""
from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from .metrics import MetricsStore

ROOT = Path(__file__).resolve().parents[2]


def parse_since(arg: str) -> float:
    if not arg:
        return 0.0
    m = re.match(r"^(\d+)([smhd])$", arg.strip())
    if not m:
        raise SystemExit(f"unparsable --since value: {arg!r} (examples: 30m, 24h, 7d)")
    n, unit = int(m.group(1)), m.group(2)
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return time.time() - n * scale


def main() -> int:
    parser = argparse.ArgumentParser(description="Renée/Aiden turn metrics report")
    parser.add_argument("--persona", default="renee", choices=["renee", "aiden"])
    parser.add_argument("--since", default="", help="e.g. 30m, 24h, 7d")
    parser.add_argument("--state-dir", default=str(ROOT / "state"))
    args = parser.parse_args()

    store = MetricsStore(Path(args.state_dir))
    since_ts = parse_since(args.since)
    summary = store.session_summary(persona=args.persona, since_ts=since_ts)
    if summary.get("turns", 0) == 0:
        scope = args.since or "ever"
        print(f"no turns recorded for {args.persona} in the last {scope}")
        return 0

    lines = [
        f"=== {args.persona} — {summary['turns']} turns ===",
        f"latency p50   {summary['latency_ms_p50']:.0f}ms",
        f"latency p95   {summary['latency_ms_p95']:.0f}ms",
        f"latency p99   {summary['latency_ms_p99']:.0f}ms",
        f"latency mean  {summary['latency_ms_mean']:.0f}ms",
        f"backends      {summary['backends']}",
        f"filter hits   {summary['filter_hits_total']} ({summary['filter_hits_per_turn']:.2f}/turn)",
        f"sycophancy    {summary['sycophancy_hits']}",
        f"retrieved avg {summary['retrieved_avg']:.1f}/turn",
        f"tokens        in={summary['input_tokens_total']} out={summary['output_tokens_total']}",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
