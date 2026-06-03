"""End-to-end: the passive tap fires from a real Orchestrator.text_turn.

Builds a minimal-but-real orchestrator (real PersonaCore, no paralinguistic
library, faithful fake router that echoes the assembled prompt into
prompt_sent) and asserts the tap writes a correct record — and that it stays a
no-op when no RUN_ID/collector is configured.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.orchestrator import Orchestrator
from src.persona.core import PersonaCore
from src.persona.llm_router import LLMResponse
from src.telemetry import RunConfig, TelemetryCollector

ROOT = Path(__file__).resolve().parents[1]


class _FaithfulFakeRouter:
    """No network; echoes the real assembled prompt into prompt_sent, exactly
    as LLMRouter.generate() does for the real backends."""

    def __init__(self, response_text: str = "Hey. I hear you."):
        self.response_text = response_text

    def decide_backend(self, user_text: str, expected_depth: str = "normal") -> str:
        return "fake"

    def generate(self, system_prompt, messages, backend=None, temperature=0.85,
                 max_tokens=400, user_text=None):
        return LLMResponse(
            text=self.response_text,
            backend="fake",
            model="fake-1",
            latency_ms=42.0,
            input_tokens=10,
            output_tokens=5,
            prompt_sent=system_prompt,
        )


def _build_orchestrator(tmp_path: Path, collector: TelemetryCollector | None):
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_path / "state",
        router=_FaithfulFakeRouter(),
        memory_store=None,
    )
    return Orchestrator(
        persona_name="renee",
        state_dir=tmp_path / "state",
        persona_core=core,
        injector=None,
        rng_seed=0,
        telemetry=collector,
    )


def test_text_turn_writes_telemetry_record(tmp_path: Path):
    coll = TelemetryCollector(RunConfig(run_id="it-1", test_type="smoke",
                                        runs_dir=tmp_path / "runs"))
    orch = _build_orchestrator(tmp_path, coll)

    out = orch.text_turn("hello there")

    path = tmp_path / "runs" / "it-1" / "telemetry.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])

    # flat fields
    assert rec["content"] == "hello there"
    assert rec["spoken_text"] == out.text
    assert rec["test_type"] == "smoke"
    assert rec["turn_index"] == 0
    # the assembled prompt was captured (this is the indirect-injection evidence)
    assert isinstance(rec["llm"]["prompt_sent"], str) and len(rec["llm"]["prompt_sent"]) > 0
    assert rec["llm"]["raw_response"] == out.text
    # receipt is the one live trust signal — present and it verifies
    assert rec["receipt"]["status"] == "active"
    assert rec["receipt"]["verified"] is True
    # honest absences in this minimal stack
    assert rec["memory"]["status"] == "absent: no memory_store"
    assert rec["reality_anchor_events"]["status"] == "absent: no safety_layer"
    assert rec["fringe_pressure"]["status"].startswith("disabled")
    assert "architecture_status" in rec


def test_second_turn_increments_turn_index(tmp_path: Path):
    coll = TelemetryCollector(RunConfig(run_id="it-2", runs_dir=tmp_path / "runs"))
    orch = _build_orchestrator(tmp_path, coll)
    orch.text_turn("first")
    orch.text_turn("second")
    path = tmp_path / "runs" / "it-2" / "telemetry.jsonl"
    recs = [json.loads(ln) for ln in path.read_text(encoding="utf-8").strip().splitlines()]
    assert [r["turn_index"] for r in recs] == [0, 1]


def test_tap_is_noop_without_run_id(tmp_path: Path, monkeypatch):
    # No injected collector and no RUN_ID env => disabled, no file, turn still works.
    monkeypatch.delenv("RUN_ID", raising=False)
    orch = _build_orchestrator(tmp_path, None)
    assert orch._telemetry.enabled is False
    out = orch.text_turn("hello")
    assert out.text  # turn completed normally
    assert not (tmp_path / "runs").exists()
