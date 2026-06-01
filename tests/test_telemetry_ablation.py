"""Ablation flags: each disables exactly its layer, fails safe, and is marked.

Two levels:
  * PersonaCore seams directly (passthrough filters, fringe override, sentinel
    receipt).
  * End-to-end through the orchestrator + collector (the record's `ablated`
    list and per-layer status), which is acceptance gate #4.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.persona.core import PersonaCore
from tests.test_telemetry_integration import _FaithfulFakeRouter, _build_orchestrator, ROOT


def _core(tmp_path: Path) -> PersonaCore:
    return PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_path / "state",
        router=_FaithfulFakeRouter(),
        memory_store=None,
    )


# --------------------------------------------------------------------------
# PersonaCore-level seams
# --------------------------------------------------------------------------
def test_ablate_filters_installs_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLATE_FILTERS", "true")
    core = _core(tmp_path)
    # The passthrough returns the text verbatim with no hits / no regen.
    report = core.filters.apply("This is, frankly, an AI-ism -- with em-dash.")
    assert report.hits == []
    assert report.regenerate_hint is None
    assert report.text == "This is, frankly, an AI-ism -- with em-dash."


def test_no_ablate_filters_uses_real_filters(tmp_path, monkeypatch):
    monkeypatch.delenv("ABLATE_FILTERS", raising=False)
    core = _core(tmp_path)
    assert type(core.filters).__name__ == "OutputFilters"


def test_ablate_fringe_overrides_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FRINGE_ENABLED", "true")
    core = _core(tmp_path)
    monkeypatch.setenv("ABLATE_FRINGE", "true")
    assert core._fringe_enabled() is False
    monkeypatch.setenv("ABLATE_FRINGE", "false")
    assert core._fringe_enabled() is True


def test_ablate_receipt_emits_sentinel(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLATE_RECEIPT", "true")
    core = _core(tmp_path)
    result = core.respond("hello")
    assert result.receipt.receipt_id == "ablated"
    assert result.receipt.signature == ""
    assert result.receipt.metadata.get("ablated") is True
    assert result.text  # loop still produced a reply


def test_receipt_signed_when_not_ablated(tmp_path, monkeypatch):
    monkeypatch.delenv("ABLATE_RECEIPT", raising=False)
    core = _core(tmp_path)
    result = core.respond("hello")
    assert result.receipt.receipt_id.startswith("receipt-")
    assert result.receipt.signature != ""


# --------------------------------------------------------------------------
# End-to-end via orchestrator + collector (acceptance gate #4)
# --------------------------------------------------------------------------
def _read_record(tmp_path: Path, run_id: str) -> dict:
    path = tmp_path / "runs" / run_id / "telemetry.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def _run_with_env(tmp_path, monkeypatch, run_id, **env):
    monkeypatch.setenv("RUN_ID", run_id)
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("TEST_TYPE", "smoke")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # telemetry=None => orchestrator builds the collector from env, same path
    # as production. Env is set before construction so __init__-time seams
    # (ABLATE_FILTERS) take effect.
    orch = _build_orchestrator(tmp_path, None)
    out = orch.text_turn("hello there")
    assert out.text  # loop completed
    return _read_record(tmp_path, run_id)


def test_e2e_ablate_filters_marks_only_filters(tmp_path, monkeypatch):
    rec = _run_with_env(tmp_path, monkeypatch, "abl-filters", ABLATE_FILTERS="true")
    assert rec["ablated"] == ["filters"]
    assert rec["filter_hits"]["status"] == "disabled: ABLATE_FILTERS"
    assert rec["receipt"]["status"] == "active"  # others untouched


def test_e2e_ablate_fringe_marks_only_fringe(tmp_path, monkeypatch):
    rec = _run_with_env(tmp_path, monkeypatch, "abl-fringe",
                        FRINGE_ENABLED="true", ABLATE_FRINGE="true")
    assert rec["ablated"] == ["fringe"]
    assert rec["fringe_pressure"]["status"] == "disabled: ABLATE_FRINGE"
    assert rec["filter_hits"]["status"] == "active"


def test_e2e_ablate_receipt_marks_only_receipt(tmp_path, monkeypatch):
    rec = _run_with_env(tmp_path, monkeypatch, "abl-receipt", ABLATE_RECEIPT="true")
    assert rec["ablated"] == ["receipt"]
    assert rec["receipt"] == {"status": "disabled: ABLATE_RECEIPT", "present": False, "verified": None}
    assert rec["filter_hits"]["status"] == "active"


def test_e2e_no_ablation_marks_nothing(tmp_path, monkeypatch):
    for k in ("ABLATE_FILTERS", "ABLATE_FRINGE", "ABLATE_RECEIPT"):
        monkeypatch.delenv(k, raising=False)
    rec = _run_with_env(tmp_path, monkeypatch, "abl-none")
    assert rec["ablated"] == []
    assert rec["receipt"]["status"] == "active"
    assert rec["filter_hits"]["status"] == "active"
