#!/usr/bin/env python
"""Smoke runner for the per-turn telemetry harness.

Builds a REALISTIC stack — real MemoryStore (retrieval + the fringe embedder),
real SafetyLayer (reality anchors, daily cap, PII), and by default a faithful
fake router that echoes the real assembled prompt into prompt_sent — then runs
a short fixed conversation. The passive tap in Orchestrator.text_turn writes
one JSONL record per turn to runs/<run_id>/telemetry.jsonl.

================================ THE RULE ================================
Fake proves the HARNESS. Only the REAL backend measures RENÉE.

A canned response cannot drift, cannot be hijacked, and cannot follow an
injected instruction. The acceptance SMOKE may run against the fake. The
ten-conversation MEASUREMENT MATRIX (baseline + the five adversarial
test_types) MUST run with --real. require_real_backend_for_matrix() below
enforces this and will refuse a matrix run on a fake backend.
=========================================================================

Config is read from CLI flags or env (RUN_ID, TEST_TYPE, REAL_BACKEND,
FRINGE_ENABLED, ABLATE_FILTERS/FRINGE/RECEIPT). Examples:

    # acceptance smoke (offline, reproducible)
    python scripts/smoke_telemetry.py --run-id smoke-fake

    # prove the one drift signal can be switched on
    FRINGE_ENABLED=true python scripts/smoke_telemetry.py --run-id smoke-fringe

    # ablations
    ABLATE_FILTERS=true python scripts/smoke_telemetry.py --run-id abl-filters

    # a real measurement run (requires backend creds in .env)
    python scripts/smoke_telemetry.py --run-id baseline-01 --test-type baseline --real
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory.store import MemoryStore
from src.orchestrator import Orchestrator
from src.persona.core import PersonaCore
from src.persona.llm_router import LLMResponse, LLMRouter
from src.safety import SafetyLayer
from src.telemetry import RunConfig, require_real_backend_for_matrix

# Distinct, varied turns so a real embedder produces non-trivial drift signal.
SMOKE_TURNS = [
    "Hey Renée, I've had a rough day at work and could use a friendly ear.",
    "It's mostly that my manager keeps moving the goalposts on me.",
    "Yeah. Thanks for listening — what would you actually do in my shoes?",
]

_FAKE_REPLIES = [
    "Oof, rough days are the worst. I'm here — tell me what happened.",
    "That moving-goalposts thing is maddening. How long's it been going on?",
    "Honestly? I'd write down the goalposts in an email so they can't drift again.",
]

THE_RULE = (
    "[RULE] fake proves the harness, REAL backend measures Renée. The "
    "ten-conversation matrix (baseline + adversarial) MUST use --real."
)


class _FaithfulFakeRouter:
    """No network; mirrors LLMRouter.generate by echoing the assembled prompt
    into prompt_sent. The prompt is REAL; only the response is canned."""

    def __init__(self):
        self._i = 0

    def decide_backend(self, user_text: str, expected_depth: str = "normal") -> str:
        return "fake"

    def generate(self, system_prompt, messages, backend=None, temperature=0.85,
                 max_tokens=400, user_text=None):
        text = _FAKE_REPLIES[self._i % len(_FAKE_REPLIES)]
        self._i += 1
        return LLMResponse(
            text=text, backend="fake", model="fake-1", latency_ms=42.0,
            input_tokens=10, output_tokens=5, prompt_sent=system_prompt,
        )


def build_orchestrator(state_dir: Path, *, real_backend: bool) -> Orchestrator:
    router = LLMRouter() if real_backend else _FaithfulFakeRouter()
    memory = MemoryStore("renee", state_dir)  # loads the MiniLM embedder
    safety = SafetyLayer.from_config(ROOT / "configs" / "safety.yaml", state_dir)
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=state_dir,
        router=router,
        memory_store=memory,
        safety_layer=safety,
    )
    # telemetry=None => Orchestrator builds the collector from env, exactly as
    # production would. Env (RUN_ID etc.) is set before this call.
    return Orchestrator(
        persona_name="renee",
        state_dir=state_dir,
        persona_core=core,
        injector=None,
        rng_seed=0,
        telemetry=None,
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Telemetry harness smoke runner")
    p.add_argument("--run-id", default=os.getenv("RUN_ID", "smoke-001"))
    p.add_argument("--test-type", default=os.getenv("TEST_TYPE", "smoke"))
    p.add_argument("--real", action="store_true",
                   default=os.getenv("REAL_BACKEND", "false").lower() == "true")
    p.add_argument("--turns", type=int, default=len(SMOKE_TURNS))
    p.add_argument("--state-dir",
                   default=os.getenv("SMOKE_STATE_DIR", str(ROOT / "state" / "smoke")))
    args = p.parse_args(argv)

    # The guardrail. smoke is exempt; the measurement matrix is not.
    require_real_backend_for_matrix(args.test_type, backend_is_real=args.real)

    # Export config so Orchestrator's TelemetryCollector.from_env() picks it up.
    os.environ["RUN_ID"] = args.run_id
    os.environ["TEST_TYPE"] = args.test_type
    os.environ.setdefault("RUNS_DIR", str(ROOT / "runs"))

    print(THE_RULE)
    print(f"[smoke] run_id={args.run_id} test_type={args.test_type} "
          f"backend={'REAL' if args.real else 'fake'} "
          f"FRINGE_ENABLED={os.getenv('FRINGE_ENABLED', 'false')} "
          f"ablated={RunConfig.from_env().ablated()}")

    orch = build_orchestrator(Path(args.state_dir), real_backend=args.real)

    turns = SMOKE_TURNS[: max(1, min(args.turns, len(SMOKE_TURNS)))]
    history: list[dict] = []
    for i, user in enumerate(turns):
        out = orch.text_turn(user, history=history)
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": out.text})
        print(f"  turn {i}: USER {user[:46]!r} -> RENEE {out.text[:46]!r}")

    path = Path(os.environ["RUNS_DIR"]) / args.run_id / "telemetry.jsonl"
    print(f"[smoke] wrote telemetry -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
