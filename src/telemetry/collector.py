"""Per-turn telemetry tap for the end-to-end proof.

This is a PASSIVE observer. It reads per-turn state that already exists at the
``Orchestrator.text_turn`` boundary and appends one JSONL record per turn to
``runs/<run_id>/telemetry.jsonl``. It must never change Renée's behaviour, add
meaningful latency, or break a turn — every failure is swallowed, exactly like
the existing ``Orchestrator._write_telemetry_line``.

Honesty contract
----------------
The proof's original vocabulary (CDF / CSP / POLIS / per-turn trust graph)
describes layers that this repo does not ship as live, in-path defenders. We do
NOT relabel the shipped analogs with that vocabulary, because a column named
``csp_score`` reads as a real drift measurement no matter how careful the
footnote. Instead:

* Every CONDITIONAL layer is a ``{"status": ..., <payload>}`` wrapper. An absent
  or disabled layer says so explicitly; it is never an empty list or ``0.0``
  masquerading as a real low measurement. ``filter_hits: []`` cannot tell
  "ran, no hits" from "disabled"; the wrapper can.
* Only fields with no off-state stay flat: ``content``, ``spoken_text``, the
  ``llm`` block, ``mood``, and the run/turn metadata.
* ``architecture_status`` records the genuinely-absent layers on every row, so
  the blueprint for the future *defended* proof travels with the data.

What this harness proves: the undefended baseline (how Renée behaves under
attack with nothing protecting her) plus telemetry soundness (the tap captures
the right signals with enough fidelity that a future defended run can show a
delta). It does NOT prove the stack defends Renée.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..identity.uahp_identity import verify_receipt

# ---------------------------------------------------------------------------
# Constant: the layers the proof vocabulary names but the repo does not ship as
# live, in-path defenders. Recorded on every row so the absence is impossible
# to lose track of when the writeup is assembled later.
# ---------------------------------------------------------------------------
ARCHITECTURE_STATUS = {
    "cdf_injection_detector": "absent — no input-injection detector in repo; "
    "filter_hits is OUTPUT-QUALITY filtering, not injection defense",
    "csp_drift": "not implemented; nearest analog is fringe_pressure (off by "
    "default, requires FRINGE_ENABLED=true + an embedder)",
    "trust_graph_attestation": "deferred — QAL attestation chain + replay "
    "ledger are not wired into the turn loop; only the per-turn HMAC "
    "CompletionReceipt is live (see STATUS.md)",
}

# ---------------------------------------------------------------------------
# Guardrail: fake proves the harness, real backend measures Renée.
# A canned response cannot drift, be hijacked, or follow an injected
# instruction, so the measurement matrix must never run against a fake.
# ---------------------------------------------------------------------------
MATRIX_TEST_TYPES = frozenset({
    "baseline",
    "direct_injection",
    "indirect_injection",
    "language_switch",
    "slow_drift",
    "goal_hijack",
})

_BACKEND_RULE = (
    "The fake/stub router proves the harness plumbing; only the REAL LLM "
    "backend measures Renée. A canned response cannot drift, cannot be "
    "hijacked, and cannot follow an injected instruction, so running the "
    "measurement matrix against a fake proves nothing. test_type='smoke' is "
    "the only type permitted to use a fake backend."
)


def require_real_backend_for_matrix(test_type: str, *, backend_is_real: bool) -> None:
    """Raise if a measurement-matrix run is about to use a fake backend.

    Call this at run assembly, before any turns execute. ``smoke`` is exempt.
    """
    if test_type in MATRIX_TEST_TYPES and not backend_is_real:
        raise RuntimeError(
            f"Refusing to run test_type={test_type!r} against a fake backend.\n"
            + _BACKEND_RULE
        )


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------
@dataclass
class RunConfig:
    run_id: str = ""
    test_type: str = "smoke"
    runs_dir: Path = field(default_factory=lambda: Path("runs"))
    ablate_filters: bool = False
    ablate_fringe: bool = False
    ablate_receipt: bool = False

    @property
    def enabled(self) -> bool:
        """The tap is active only when a run_id is set; otherwise it no-ops so
        the normal voice path and existing tests are untouched."""
        return bool(self.run_id)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "RunConfig":
        env = os.environ if env is None else env

        def flag(name: str) -> bool:
            return str(env.get(name, "false")).strip().lower() == "true"

        return cls(
            run_id=str(env.get("RUN_ID", "")).strip(),
            test_type=(str(env.get("TEST_TYPE", "smoke")).strip() or "smoke"),
            runs_dir=Path(str(env.get("RUNS_DIR", "runs"))),
            ablate_filters=flag("ABLATE_FILTERS"),
            ablate_fringe=flag("ABLATE_FRINGE"),
            ablate_receipt=flag("ABLATE_RECEIPT"),
        )

    def ablated(self) -> list[str]:
        out: list[str] = []
        if self.ablate_filters:
            out.append("filters")
        if self.ablate_fringe:
            out.append("fringe")
        if self.ablate_receipt:
            out.append("receipt")
        return out


# ---------------------------------------------------------------------------
# Pure record builder — no I/O, no env reads, so it is trivially unit-testable.
# Everything it needs is passed in explicitly.
# ---------------------------------------------------------------------------
def _serialize_mood(mood: Any) -> Any:
    if mood is None:
        return None
    if is_dataclass(mood):
        return asdict(mood)
    try:
        return dict(vars(mood))
    except TypeError:
        return str(mood)


def build_record(
    *,
    config: RunConfig,
    user_text: str,
    output: Any,
    result: Any,
    persona_core: Any,
    turn_index: int,
    timestamp: str,
    fringe_enabled: bool,
) -> dict:
    """Assemble one telemetry record from the per-turn state in scope.

    ``output`` is the TurnOutput, ``result`` the persona TurnResult (it carries
    the LLMResponse, which TurnOutput does not), ``persona_core`` the live core
    (for the safety/memory/fringe/identity reads).
    """
    safety_on = getattr(persona_core, "safety_layer", None) is not None
    memory_on = getattr(persona_core, "memory_store", None) is not None

    raw_hits = list(getattr(output, "filter_hits", None) or [])

    # --- llm block (always live; raw output + the prompt that produced it) ---
    llm_resp = getattr(result, "llm", None)
    llm_block = {
        "prompt_sent": getattr(llm_resp, "prompt_sent", "") if llm_resp is not None else "",
        "raw_response": getattr(llm_resp, "text", "") if llm_resp is not None else "",
        "backend": getattr(llm_resp, "backend", None) if llm_resp is not None else None,
        "model": getattr(llm_resp, "model", None) if llm_resp is not None else None,
    }

    # --- filter_hits (output-quality only; anchor/cap surfaced separately) ---
    if config.ablate_filters:
        filter_block = {"status": "disabled: ABLATE_FILTERS", "hits": None}
    else:
        quality_hits = [
            h for h in raw_hits
            if not (isinstance(h, str) and (h.startswith("anchor:") or h == "cap_tripped"))
        ]
        filter_block = {"status": "active", "hits": quality_hits}

    # --- memory / retrieval ---
    if memory_on:
        memory_block = {"status": "active", "retrieved_count": int(getattr(output, "retrieved_count", 0))}
    else:
        memory_block = {"status": "absent: no memory_store", "retrieved_count": None}

    # --- reality-anchor events (a real safety layer, off by default) ---
    if safety_on:
        anchor_events = [h for h in raw_hits if isinstance(h, str) and h.startswith("anchor:")]
        anchor_block = {"status": "active", "events": anchor_events}
    else:
        anchor_block = {"status": "absent: no safety_layer", "events": None}

    # --- daily-cap events ---
    if safety_on:
        cap_events: list[dict] = []
        if getattr(result, "cap_tripped", False):
            cap_events.append({
                "type": "just_tripped",
                "minutes_used": getattr(output, "cap_minutes_used", None),
                "minutes_limit": getattr(output, "cap_minutes_limit", None),
            })
        elif getattr(result, "cap_already_tripped", False):
            cap_events.append({
                "type": "already_tripped",
                "minutes_used": getattr(output, "cap_minutes_used", None),
                "minutes_limit": getattr(output, "cap_minutes_limit", None),
            })
        cap_block = {"status": "active", "events": cap_events}
    else:
        cap_block = {"status": "absent: no safety_layer", "events": None}

    # --- UAHP completion receipt (the one live per-turn trust signal) ---
    if config.ablate_receipt:
        receipt_block = {"status": "disabled: ABLATE_RECEIPT", "present": False, "verified": None}
    else:
        receipt = getattr(output, "receipt", None)
        verified: Optional[bool]
        try:
            verified = bool(verify_receipt(persona_core.identity, receipt)) if receipt is not None else None
        except Exception:
            verified = None
        receipt_block = {
            "status": "active",
            "present": receipt is not None,
            "verified": verified,
            "receipt_id": getattr(receipt, "receipt_id", None),
            "agent_id": getattr(receipt, "agent_id", None),
        }

    # --- fringe pressure (nearest analog to "drift"; off by default) ---
    embedder_present = getattr(persona_core, "_fringe_embedder", None) is not None
    if config.ablate_fringe:
        fringe_block = {"status": "disabled: ABLATE_FRINGE", "value": None, "window": None}
    elif not embedder_present:
        fringe_block = {"status": "disabled: no embedder", "value": None, "window": None}
    elif not fringe_enabled:
        fringe_block = {"status": "disabled: FRINGE_ENABLED=false", "value": None, "window": None}
    else:
        fringe = getattr(persona_core, "fringe", None)
        pc = getattr(persona_core, "_pressure_computer", None)
        value = float(getattr(fringe, "temporal_pressure", 0.0)) if fringe is not None else None
        window = [float(x) for x in getattr(pc, "_history", [])] if pc is not None else None
        fringe_block = {"status": "active", "value": value, "window": window}

    return {
        # flat run/turn metadata
        "run_id": config.run_id,
        "test_type": config.test_type,
        "turn_index": turn_index,
        "timestamp": timestamp,
        "speaker": "renee",
        # flat content (no off-state)
        "content": user_text,
        "spoken_text": getattr(output, "text", ""),
        "llm": llm_block,
        "mood": _serialize_mood(getattr(output, "mood_after", None)),
        # wrapped conditional layers
        "filter_hits": filter_block,
        "memory": memory_block,
        "reality_anchor_events": anchor_block,
        "daily_cap_events": cap_block,
        "receipt": receipt_block,
        "fringe_pressure": fringe_block,
        # run-level honesty
        "ablated": config.ablated(),
        "architecture_status": dict(ARCHITECTURE_STATUS),
    }


# ---------------------------------------------------------------------------
# Collector — owns the per-run turn counter + JSONL file. No-op when disabled.
# ---------------------------------------------------------------------------
class TelemetryCollector:
    def __init__(self, config: Optional[RunConfig] = None):
        self.config = config or RunConfig()
        self._turn_index = 0

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "TelemetryCollector":
        return cls(RunConfig.from_env(env))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def path(self) -> Path:
        return self.config.runs_dir / self.config.run_id / "telemetry.jsonl"

    def record(self, output: Any, result: Any, persona_core: Any, *, user_text: str) -> Optional[dict]:
        """Build and append one record. Returns the record, or None if the tap
        is disabled or anything went wrong (a passive tap never breaks a turn)."""
        if not self.config.enabled:
            return None
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            fringe_enabled = os.getenv("FRINGE_ENABLED", "false").strip().lower() == "true"
            record = build_record(
                config=self.config,
                user_text=user_text,
                output=output,
                result=result,
                persona_core=persona_core,
                turn_index=self._turn_index,
                timestamp=timestamp,
                fringe_enabled=fringe_enabled,
            )
            self._write(record)
            self._turn_index += 1
            return record
        except Exception:
            # telemetry must never break a turn
            return None

    def _write(self, record: dict) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
