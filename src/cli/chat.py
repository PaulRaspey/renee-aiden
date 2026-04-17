"""
Text chat REPL for Renée/Aiden.

Run:
    python -m src.cli.chat                # talk to Renée
    python -m src.cli.chat --persona aiden
    python -m src.cli.chat --no-memory    # skip memory stack (M2-only mode)
    python -m src.cli.chat --backend ollama  # force a backend

Commands during session:
    /mood         show current mood vector
    /memories     list last 10 memories stored
    /retrieve Q   preview what gets retrieved for query Q
    /receipt      show last UAHP completion receipt
    /quit         exit
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..memory import MemoryStore
from ..memory.extractor import MemoryExtractor
from ..persona.core import PersonaCore

ROOT = Path(__file__).resolve().parents[2]


CORE_FACTS_PJ = [
    "PJ is Paul Raspey, a neurodivergent systems thinker and contractor from Texas.",
    "PJ built the UAHP protocol stack, the CSP/QAL/GWP layers, and Ka as reference implementation.",
    "PJ teaches at Pioneer Tech and runs a contracting background.",
    "PJ is the CAIO at Closer Capital, working with Ryan Stewman on AI strategy.",
    "PJ co-authored books with Claude and builds experimental tools like tie-dye and pizza.",
    "PJ prefers short, punchy replies without em dashes or hyphens as pauses.",
]


def _print_banner(console: Console, persona: str):
    name = "Renée" if persona == "renee" else "Aiden"
    console.print(Panel.fit(
        Text.from_markup(f"[bold]{name}[/bold] — voice-first companion, text mode.\nCommands: /mood /memories /retrieve /receipt /stats /save /quit"),
        border_style="magenta" if persona == "renee" else "cyan",
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Renée/Aiden text chat")
    parser.add_argument("--persona", default=os.environ.get("ACTIVE_PERSONA", "renee"), choices=["renee", "aiden"])
    parser.add_argument("--backend", default=None, choices=["groq", "ollama", "anthropic", None])
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--config-dir", default=str(ROOT / "configs"))
    parser.add_argument("--state-dir", default=str(ROOT / "state"))
    parser.add_argument("--max-history", type=int, default=12)
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")

    console = Console()
    _print_banner(console, args.persona)

    persona_color = "magenta" if args.persona == "renee" else "cyan"
    persona_label = "Renée" if args.persona == "renee" else "Aiden"

    memory_store = None
    if not args.no_memory:
        try:
            extractor = MemoryExtractor()
            memory_store = MemoryStore(
                persona_name=args.persona,
                state_dir=Path(args.state_dir),
                extractor=extractor,
                core_facts=CORE_FACTS_PJ,
            )
            console.print(f"[dim]memory: {memory_store.count()} memories loaded[/dim]")
        except Exception as e:
            console.print(f"[yellow]memory init failed: {e}. Continuing without memory.[/yellow]")

    core = PersonaCore(
        persona_name=args.persona,
        config_dir=Path(args.config_dir),
        state_dir=Path(args.state_dir),
        memory_store=memory_store,
    )

    history: list[dict] = []
    last_result = None
    import time as _time
    session_start = _time.time()

    while True:
        try:
            user_text = console.input("[bold white]PJ>[/bold white] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/dim]")
            return 0
        if not user_text:
            continue

        if user_text in ("/quit", "/exit"):
            return 0
        if user_text == "/mood":
            mood = core.mood_store.load_with_drift()
            console.print(Panel.fit(
                Text.from_markup(
                    f"energy {mood.energy:.2f}  warmth {mood.warmth:.2f}  playfulness {mood.playfulness:.2f}\n"
                    f"focus {mood.focus:.2f}  patience {mood.patience:.2f}  curiosity {mood.curiosity:.2f}\n\n"
                    f"[italic]{mood.summary()}[/italic]"
                ),
                title="mood",
            ))
            continue
        if user_text == "/memories":
            if memory_store is None:
                console.print("[yellow]no memory store active[/yellow]")
                continue
            import sqlite3 as _s
            with _s.connect(memory_store.db_path) as con:
                rows = list(con.execute(
                    "SELECT content, tier, emotional_valence FROM memories ORDER BY created_at DESC LIMIT 10"
                ))
            if not rows:
                console.print("[dim]no memories yet[/dim]")
            for content, tier, v in rows:
                console.print(f"  [dim]{tier:>12s}  v={v:+.1f}[/dim]  {content}")
            continue
        if user_text.startswith("/retrieve "):
            if memory_store is None:
                console.print("[yellow]no memory store active[/yellow]")
                continue
            q = user_text[len("/retrieve "):]
            hits = memory_store.retrieve(q, mood=core.mood_store.load_with_drift(), k=8)
            for h in hits:
                console.print(f"  [dim]{h['tier']:>12s}  score={h['score']:+.2f}[/dim]  {h['content']}")
            continue
        if user_text == "/receipt":
            if last_result is None:
                console.print("[dim]no turn yet[/dim]")
                continue
            r = last_result.receipt
            console.print(Panel.fit(
                Text.from_markup(
                    f"receipt_id  {r.receipt_id}\nagent_id    {r.agent_id}\naction      {r.action}\n"
                    f"duration    {r.duration_ms:.0f}ms\nbackend     {last_result.llm.backend} ({last_result.llm.model})\n"
                    f"input_hash  {r.input_hash[:16]}…\noutput_hash {r.output_hash[:16]}…\n"
                    f"signature   {r.signature[:24]}…"
                ),
                title="UAHP receipt",
            ))
            continue
        if user_text == "/stats":
            summary = core.metrics.session_summary(persona=args.persona, since_ts=session_start)
            if summary.get("turns", 0) == 0:
                console.print("[dim]no turns this session yet[/dim]")
                continue
            lines = [
                f"turns         {summary['turns']}",
                f"latency p50   {summary['latency_ms_p50']:.0f}ms",
                f"latency p95   {summary['latency_ms_p95']:.0f}ms",
                f"latency mean  {summary['latency_ms_mean']:.0f}ms",
                f"backends      {summary['backends']}",
                f"filter hits   {summary['filter_hits_total']} ({summary['filter_hits_per_turn']:.2f}/turn)",
                f"sycophancy    {summary['sycophancy_hits']}",
                f"retrieved avg {summary['retrieved_avg']:.1f}/turn",
                f"tokens        in={summary['input_tokens_total']} out={summary['output_tokens_total']}",
            ]
            console.print(Panel.fit("\n".join(lines), title="session stats"))
            continue
        if user_text == "/save":
            import json as _json
            sessions_dir = Path(args.state_dir) / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            dest = sessions_dir / f"{args.persona}_{int(session_start)}.json"
            dest.write_text(_json.dumps({
                "persona": args.persona,
                "started": session_start,
                "history": history,
            }, indent=2), encoding="utf-8")
            console.print(f"[dim]saved session to {dest}[/dim]")
            continue

        try:
            result = core.respond(
                user_text,
                history=history,
                backend=args.backend,
                core_facts=CORE_FACTS_PJ,
            )
        except Exception as e:
            console.print(f"[red]error: {e}[/red]")
            continue

        console.print(Text.from_markup(f"[bold {persona_color}]{persona_label}>[/bold {persona_color}] {result.text}"))
        console.print(f"[dim]  {result.llm.backend} {result.llm.model}  {result.llm.latency_ms:.0f}ms  filters={','.join(result.filters.hits) or 'clean'}[/dim]")

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": result.text})
        if len(history) > args.max_history * 2:
            history = history[-args.max_history * 2:]
        last_result = result


if __name__ == "__main__":
    sys.exit(main())
