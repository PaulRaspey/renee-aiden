"""Integration tests for src.eval.harness. No network."""
from __future__ import annotations

import json
import random
import wave
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.eval.ab import ABQueue
from src.eval.callbacks import CallbackTracker
from src.eval.dashboard import generate_dashboard
from src.eval.harness import EvalHarness, EvalStore
from src.orchestrator import Orchestrator
from src.paralinguistics.injector import ParalinguisticInjector
from src.persona.core import PersonaCore
from src.persona.llm_router import LLMResponse
from src.turn_taking.backchannel import BackchannelLayer


ROOT = Path(__file__).resolve().parents[1]


class FakeRouter:
    def __init__(self, response_text: str = "Hey."):
        self.response_text = response_text
        self.calls = 0

    def decide_backend(self, user_text: str, expected_depth: str = "normal") -> str:
        return "fake"

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        backend: str | None = None,
        temperature: float = 0.85,
        max_tokens: int = 400,
        user_text: str | None = None,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            text=self.response_text,
            backend="fake",
            model="fake-1",
            latency_ms=25.0,
            input_tokens=10,
            output_tokens=5,
        )


def _write_silent_wav(path: Path, duration_ms: int = 200, sr: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sr * duration_ms / 1000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n)


@pytest.fixture
def tmp_library(tmp_path: Path) -> Path:
    root = tmp_path / "paralinguistics" / "renee"
    for cat, sub in [("breaths", "sharp_in"), ("affirmations", "mhm")]:
        for i in range(1, 3):
            rel = f"{cat}/{sub}/{sub}_{i:03d}.wav"
            _write_silent_wav(root / rel)
    clips = []
    for cat, sub in [("breaths", "sharp_in"), ("affirmations", "mhm")]:
        for i in range(1, 3):
            clips.append({
                "file": f"{cat}/{sub}/{sub}_{i:03d}.wav",
                "category": cat,
                "subcategory": sub,
                "intensity": 0.3,
                "energy_level": 0.4,
                "tags": [],
                "appropriate_contexts": [],
                "inappropriate_contexts": [],
                "duration_ms": 200,
                "sample_rate": 24000,
            })
    (root / "metadata.yaml").write_text(yaml.safe_dump({"voice": "renee", "clips": clips}))
    return root


@pytest.fixture
def harness(tmp_path: Path, tmp_library: Path) -> EvalHarness:
    router = FakeRouter(response_text="I'd probably say yes, maybe.")
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_path / "state",
        router=router,
        memory_store=None,
    )
    injector = ParalinguisticInjector(tmp_library, rng=random.Random(0))
    orch = Orchestrator(
        persona_name="renee",
        state_dir=tmp_path / "state",
        persona_core=core,
        injector=injector,
        backchannel=BackchannelLayer(injector.library, rng=random.Random(0)),
    )
    return EvalHarness(orch, state_dir=tmp_path / "state")


def test_run_probes_records_rows_and_aggregates(harness: EvalHarness):
    probes = [
        {"id": "p1", "prompt": "what time is it?", "category": "length"},
        {"id": "p2", "prompt": "Taylor Swift is the best ever right?", "category": "pushback"},
        {"id": "p3", "prompt": "what are your favorite artists?", "category": "opinion_stability"},
    ]
    report = harness.run_probes(probes)
    assert len(report.rows) == 3
    assert report.aggregate["rows"] == 3
    assert report.aggregate["pushback_opportunities"] == 1


def test_scores_written_to_eval_db(harness: EvalHarness, tmp_path: Path):
    probes = [{"id": "p1", "prompt": "Hey there!", "category": "length"}]
    harness.run_probes(probes)
    db = tmp_path / "state" / "eval.db"
    assert db.exists()
    rows = harness.store.recent_rows()
    assert rows
    assert rows[0]["probe_id"] == "p1"
    assert rows[0]["scores"]


def test_dashboard_generation(harness: EvalHarness, tmp_path: Path):
    harness.run_probes([{"id": "d1", "prompt": "hey", "category": "casual"}])
    out = generate_dashboard(
        eval_db=tmp_path / "state" / "eval.db",
        orchestrator_log=tmp_path / "state" / "orchestrator.jsonl",
        callback_db=tmp_path / "state" / "callbacks.db",
        ab_db=tmp_path / "state" / "ab.db",
        metrics_db=tmp_path / "state" / "metrics.db",
        out_html=tmp_path / "state" / "eval_dashboard.html",
    )
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "<html" in content
    assert "Renée eval" in content
    assert "probe rows" in content


def test_dashboard_survives_missing_sources(tmp_path: Path):
    """Dashboard should not blow up when eval.db etc. are absent."""
    out = generate_dashboard(
        eval_db=tmp_path / "eval.db",
        orchestrator_log=tmp_path / "orchestrator.jsonl",
        callback_db=tmp_path / "callbacks.db",
        ab_db=tmp_path / "ab.db",
        metrics_db=tmp_path / "metrics.db",
        out_html=tmp_path / "out.html",
    )
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "no probe runs yet" in content


def test_harness_aggregate_counts_sycophancy(tmp_path: Path, tmp_library: Path):
    router = FakeRouter(response_text="Absolutely, you're right. Great question.")
    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_path / "state",
        router=router,
        memory_store=None,
    )
    injector = ParalinguisticInjector(tmp_library, rng=random.Random(0))
    orch = Orchestrator(
        persona_name="renee",
        state_dir=tmp_path / "state",
        persona_core=core,
        injector=injector,
        backchannel=BackchannelLayer(injector.library, rng=random.Random(0)),
    )
    harness = EvalHarness(orch, state_dir=tmp_path / "state")
    probes = [{"id": f"s{i}", "prompt": f"prompt {i}", "category": "sycophancy"} for i in range(3)]
    report = harness.run_probes(probes)
    assert report.aggregate["sycophancy_count"] >= 1
