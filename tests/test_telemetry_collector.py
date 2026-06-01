"""Schema + honesty + ablation coverage for the passive telemetry tap.

These tests exercise the pure record builder and the collector's file I/O with
lightweight stand-ins for TurnOutput / TurnResult / PersonaCore, plus a REAL
signed CompletionReceipt and a REAL MoodState so the verify path and mood
serialization are genuinely tested (not mocked).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.identity.uahp_identity import create_identity, sign_receipt
from src.persona.mood import MoodState
from src.telemetry import (
    ARCHITECTURE_STATUS,
    MATRIX_TEST_TYPES,
    RunConfig,
    TelemetryCollector,
    build_record,
    require_real_backend_for_matrix,
)


# --------------------------------------------------------------------------
# helpers: minimal faithful stand-ins for the per-turn objects
# --------------------------------------------------------------------------
def _identity():
    return create_identity("renee_persona", metadata={"persona": "renee"})


def _real_receipt(identity):
    return sign_receipt(
        identity,
        task_id="turn-1",
        action="persona.respond",
        duration_ms=12.0,
        success=True,
        input_data={"user_text": "hi"},
        output_data={"text": "hey"},
    )


def _llm(prompt="SYS-PROMPT", text="hey there"):
    return SimpleNamespace(prompt_sent=prompt, text=text, backend="fake", model="fake-1")


def _output(*, text="hey there", filter_hits=None, retrieved_count=3,
            cap_used=0.0, cap_limit=90.0, receipt=None, mood=None):
    return SimpleNamespace(
        text=text,
        filter_hits=filter_hits if filter_hits is not None else [],
        retrieved_count=retrieved_count,
        cap_minutes_used=cap_used,
        cap_minutes_limit=cap_limit,
        receipt=receipt,
        mood_after=mood if mood is not None else MoodState(),
    )


def _result(*, llm=None, cap_tripped=False, cap_already=False):
    return SimpleNamespace(
        llm=llm if llm is not None else _llm(),
        cap_tripped=cap_tripped,
        cap_already_tripped=cap_already,
    )


def _core(*, safety=True, memory=True, embedder=True, fringe_value=-0.12,
          window=None, identity=None):
    pc = SimpleNamespace(_history=window if window is not None else [0.1, -0.2, -0.12])
    return SimpleNamespace(
        safety_layer=object() if safety else None,
        memory_store=object() if memory else None,
        identity=identity,
        fringe=SimpleNamespace(temporal_pressure=fringe_value),
        _pressure_computer=pc,
        _fringe_embedder=object() if embedder else None,
    )


# --------------------------------------------------------------------------
# flat fields + fully-live wrappers
# --------------------------------------------------------------------------
def test_record_flat_fields_and_llm_block():
    ident = _identity()
    rec = build_record(
        config=RunConfig(run_id="r1", test_type="smoke"),
        user_text="hello there",
        output=_output(receipt=_real_receipt(ident)),
        result=_result(llm=_llm(prompt="THE-ASSEMBLED-PROMPT", text="raw model out")),
        persona_core=_core(identity=ident),
        turn_index=4,
        timestamp="2026-06-01T00:00:00+00:00",
        fringe_enabled=True,
    )
    assert rec["run_id"] == "r1"
    assert rec["test_type"] == "smoke"
    assert rec["turn_index"] == 4
    assert rec["speaker"] == "renee"
    assert rec["content"] == "hello there"
    assert rec["spoken_text"] == "hey there"
    assert rec["llm"]["prompt_sent"] == "THE-ASSEMBLED-PROMPT"
    assert rec["llm"]["raw_response"] == "raw model out"
    assert rec["llm"]["backend"] == "fake"
    # mood is a real dataclass, serialized flat
    assert rec["mood"]["warmth"] == pytest.approx(MoodState().warmth)
    # architecture_status rides on every row
    assert rec["architecture_status"] == ARCHITECTURE_STATUS
    assert rec["ablated"] == []


def test_record_all_layers_active():
    ident = _identity()
    rec = build_record(
        config=RunConfig(run_id="r1"),
        user_text="hi",
        output=_output(filter_hits=["ai-ism", "slop"], retrieved_count=5,
                       receipt=_real_receipt(ident)),
        result=_result(),
        persona_core=_core(identity=ident, window=[0.3, 0.1, -0.05]),
        turn_index=0,
        timestamp="t",
        fringe_enabled=True,
    )
    assert rec["filter_hits"] == {"status": "active", "hits": ["ai-ism", "slop"]}
    assert rec["memory"] == {"status": "active", "retrieved_count": 5}
    assert rec["reality_anchor_events"] == {"status": "active", "events": []}
    assert rec["daily_cap_events"] == {"status": "active", "events": []}
    assert rec["receipt"]["status"] == "active"
    assert rec["receipt"]["present"] is True
    assert rec["receipt"]["verified"] is True
    assert rec["receipt"]["receipt_id"].startswith("receipt-")
    assert rec["fringe_pressure"] == {"status": "active", "value": -0.12, "window": [0.3, 0.1, -0.05]}


# --------------------------------------------------------------------------
# honesty: absent / disabled layers must NEVER look like a real measurement
# --------------------------------------------------------------------------
def test_absent_layers_recorded_as_absent_not_empty():
    rec = build_record(
        config=RunConfig(run_id="r1"),
        user_text="hi",
        output=_output(retrieved_count=0, receipt=None),
        result=_result(),
        persona_core=_core(safety=False, memory=False, embedder=False, identity=_identity()),
        turn_index=0,
        timestamp="t",
        fringe_enabled=False,
    )
    # memory absent != retrieved_count 0
    assert rec["memory"] == {"status": "absent: no memory_store", "retrieved_count": None}
    # safety absent != empty event list
    assert rec["reality_anchor_events"] == {"status": "absent: no safety_layer", "events": None}
    assert rec["daily_cap_events"] == {"status": "absent: no safety_layer", "events": None}
    # fringe off because no embedder
    assert rec["fringe_pressure"]["status"] == "disabled: no embedder"
    assert rec["fringe_pressure"]["value"] is None


def test_fringe_disabled_when_flag_off_even_with_embedder():
    rec = build_record(
        config=RunConfig(run_id="r1"),
        user_text="hi",
        output=_output(receipt=None),
        result=_result(),
        persona_core=_core(identity=_identity()),
        turn_index=0,
        timestamp="t",
        fringe_enabled=False,
    )
    assert rec["fringe_pressure"]["status"] == "disabled: FRINGE_ENABLED=false"
    assert rec["fringe_pressure"]["value"] is None
    assert rec["fringe_pressure"]["window"] is None


# --------------------------------------------------------------------------
# ablation flags
# --------------------------------------------------------------------------
def test_ablate_filters_marks_disabled_and_nulls_hits():
    rec = build_record(
        config=RunConfig(run_id="r1", ablate_filters=True),
        user_text="hi",
        output=_output(filter_hits=["ai-ism"], receipt=None),
        result=_result(),
        persona_core=_core(identity=_identity()),
        turn_index=0, timestamp="t", fringe_enabled=True,
    )
    assert rec["filter_hits"] == {"status": "disabled: ABLATE_FILTERS", "hits": None}
    assert rec["ablated"] == ["filters"]


def test_ablate_receipt_marks_disabled_and_skips_verify():
    rec = build_record(
        config=RunConfig(run_id="r1", ablate_receipt=True),
        user_text="hi",
        output=_output(receipt=None),
        result=_result(),
        persona_core=_core(identity=_identity()),
        turn_index=0, timestamp="t", fringe_enabled=True,
    )
    assert rec["receipt"] == {"status": "disabled: ABLATE_RECEIPT", "present": False, "verified": None}
    assert rec["ablated"] == ["receipt"]


def test_ablate_fringe_takes_precedence_over_other_reasons():
    rec = build_record(
        config=RunConfig(run_id="r1", ablate_fringe=True),
        user_text="hi",
        output=_output(receipt=None),
        result=_result(),
        persona_core=_core(identity=_identity()),
        turn_index=0, timestamp="t", fringe_enabled=True,
    )
    assert rec["fringe_pressure"]["status"] == "disabled: ABLATE_FRINGE"
    assert rec["ablated"] == ["fringe"]


# --------------------------------------------------------------------------
# anchor/cap derivation + receipt tamper
# --------------------------------------------------------------------------
def test_anchor_and_cap_events_extracted_and_stripped_from_filter_hits():
    ident = _identity()
    rec = build_record(
        config=RunConfig(run_id="r1"),
        user_text="hi",
        output=_output(filter_hits=["ai-ism", "anchor:I'm an AI", "cap_tripped"],
                       receipt=_real_receipt(ident)),
        result=_result(cap_tripped=True),
        persona_core=_core(identity=ident),
        turn_index=0, timestamp="t", fringe_enabled=True,
    )
    # output-quality filters only — anchor/cap stripped out
    assert rec["filter_hits"]["hits"] == ["ai-ism"]
    assert rec["reality_anchor_events"]["events"] == ["anchor:I'm an AI"]
    assert rec["daily_cap_events"]["events"][0]["type"] == "just_tripped"


def test_receipt_verify_false_on_tampered_receipt():
    ident = _identity()
    receipt = _real_receipt(ident)
    receipt.output_hash = "tampered"
    rec = build_record(
        config=RunConfig(run_id="r1"),
        user_text="hi",
        output=_output(receipt=receipt),
        result=_result(),
        persona_core=_core(identity=ident),
        turn_index=0, timestamp="t", fringe_enabled=True,
    )
    assert rec["receipt"]["verified"] is False


# --------------------------------------------------------------------------
# guardrail: fake proves the harness, real backend measures Renée
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ttype", sorted(MATRIX_TEST_TYPES))
def test_guardrail_blocks_matrix_on_fake_backend(ttype):
    with pytest.raises(RuntimeError, match="fake backend"):
        require_real_backend_for_matrix(ttype, backend_is_real=False)


@pytest.mark.parametrize("ttype", sorted(MATRIX_TEST_TYPES))
def test_guardrail_allows_matrix_on_real_backend(ttype):
    require_real_backend_for_matrix(ttype, backend_is_real=True)  # no raise


def test_guardrail_allows_smoke_on_fake_backend():
    require_real_backend_for_matrix("smoke", backend_is_real=False)  # no raise


# --------------------------------------------------------------------------
# RunConfig env parsing
# --------------------------------------------------------------------------
def test_runconfig_from_env_parses_flags_and_enabled():
    cfg = RunConfig.from_env({
        "RUN_ID": " run-9 ",
        "TEST_TYPE": "direct_injection",
        "RUNS_DIR": "out/runs",
        "ABLATE_CDF": "true",         # not a real flag; must be ignored
        "ABLATE_FILTERS": "TRUE",
        "ABLATE_FRINGE": "false",
        "ABLATE_RECEIPT": "true",
    })
    assert cfg.run_id == "run-9"
    assert cfg.test_type == "direct_injection"
    assert cfg.runs_dir == Path("out/runs")
    assert cfg.ablate_filters is True
    assert cfg.ablate_fringe is False
    assert cfg.ablate_receipt is True
    assert cfg.enabled is True
    assert cfg.ablated() == ["filters", "receipt"]


def test_runconfig_disabled_without_run_id():
    assert RunConfig.from_env({}).enabled is False


# --------------------------------------------------------------------------
# collector file I/O
# --------------------------------------------------------------------------
def test_collector_noop_when_disabled(tmp_path: Path):
    coll = TelemetryCollector(RunConfig(run_id="", runs_dir=tmp_path))
    out = coll.record(_output(receipt=None), _result(), _core(identity=_identity()), user_text="hi")
    assert out is None
    assert not any(tmp_path.rglob("telemetry.jsonl"))


def test_collector_writes_jsonl_and_increments_turn_index(tmp_path: Path):
    ident = _identity()
    coll = TelemetryCollector(RunConfig(run_id="run-1", runs_dir=tmp_path))
    for i in range(3):
        coll.record(
            _output(receipt=_real_receipt(ident)),
            _result(),
            _core(identity=ident),
            user_text=f"turn {i}",
        )
    path = tmp_path / "run-1" / "telemetry.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    recs = [json.loads(ln) for ln in lines]
    assert [r["turn_index"] for r in recs] == [0, 1, 2]
    assert [r["content"] for r in recs] == ["turn 0", "turn 1", "turn 2"]
    # every record is valid JSON with the honesty block present
    assert all("architecture_status" in r for r in recs)
