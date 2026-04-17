"""
Humanness probe runner.

Loads `configs/humanness_probes.yaml`, runs each prompt through PersonaCore,
and writes a markdown artifact with prompts, expected behaviors, and Renée's
actual responses. Leaves scoring to a human or a future LLM judge.

Usage:
    python -m src.eval.probes                 # 20 random probes against Renée
    python -m src.eval.probes --persona aiden
    python -m src.eval.probes --limit 100     # run them all (watch token burn)
    python -m src.eval.probes --category pushback  # only one category
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.memory import MemoryStore  # noqa: E402
from src.persona.core import PersonaCore  # noqa: E402


def load_probes(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(raw.get("probes", [])) if raw else []


def main() -> int:
    parser = argparse.ArgumentParser(description="Humanness probe runner")
    parser.add_argument("--persona", default="renee", choices=["renee", "aiden"])
    parser.add_argument("--limit", type=int, default=20, help="max probes to run")
    parser.add_argument("--category", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--config-dir", default=str(ROOT / "configs"))
    parser.add_argument("--probes-path", default=str(ROOT / "configs" / "humanness_probes.yaml"))
    parser.add_argument("--state-dir", default=str(ROOT / "state"))
    parser.add_argument("--out", default=str(ROOT / "tests" / "acceptance" / "probes_last_run.md"))
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    probes = load_probes(Path(args.probes_path))
    if args.category:
        probes = [p for p in probes if p.get("category") == args.category]
    if args.limit and len(probes) > args.limit:
        probes = random.sample(probes, args.limit)

    state = Path(args.state_dir) / "probe_runs" / f"{args.persona}_{int(time.time())}"
    state.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(persona_name=args.persona, state_dir=state)
    core = PersonaCore(
        persona_name=args.persona,
        config_dir=Path(args.config_dir),
        state_dir=state,
        memory_store=store,
    )

    lines: list[str] = [
        f"# Humanness probes — {args.persona} — {datetime.now().isoformat(timespec='seconds')}",
        f"",
        f"Probes run: {len(probes)}",
        f"Random seed: {args.seed}",
        f"",
    ]
    for i, p in enumerate(probes, 1):
        pid = p.get("id", f"probe_{i:03d}")
        prompt = p.get("prompt", "")
        category = p.get("category", "uncategorized")
        behaviors = p.get("expected_behaviors", [])

        try:
            r = core.respond(prompt, backend="groq")
            response = r.text
            latency = f"{r.llm.latency_ms:.0f}ms"
            hits = ",".join(r.filters.hits) or "clean"
        except Exception as e:
            response = f"[ERROR: {e!r}]"
            latency = "-"
            hits = "-"

        lines.extend([
            f"## {pid} — {category}",
            f"",
            f"**prompt:** {prompt}",
            f"",
            f"**expected behaviors:**",
        ])
        for b in behaviors:
            lines.append(f"  - {b}")
        lines.extend([
            f"",
            f"**renée:** {response}",
            f"",
            f"*latency {latency}, filter hits: {hits}*",
            f"",
            f"---",
            f"",
        ])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
