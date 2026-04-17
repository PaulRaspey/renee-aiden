"""Telemetry store round-trips and summary aggregation."""
import time
from pathlib import Path

from src.eval.metrics import MetricsStore, TurnMetric


def _mk(ts: float, backend: str = "groq", latency: float = 1000.0, filter_hits=None, syc=False) -> TurnMetric:
    return TurnMetric(
        ts=ts,
        persona="renee",
        backend=backend,
        model="qwen/qwen3-32b",
        latency_ms=latency,
        input_tokens=500,
        output_tokens=80,
        filter_hits=filter_hits or [],
        regen=False,
        sycophancy_flag=syc,
        retrieved_count=3,
        user_chars=40,
        response_chars=200,
        mood_json="{}",
        receipt_id=f"r-{int(ts)}",
    )


def test_record_and_summary(tmp_path: Path):
    store = MetricsStore(tmp_path)
    t0 = time.time()
    for i, lat in enumerate([500, 800, 1200, 1600, 2400], start=0):
        store.record_turn(_mk(t0 + i, latency=lat, filter_hits=["slop:1"] if i == 2 else []))
    summary = store.session_summary(persona="renee", since_ts=t0 - 1)
    assert summary["turns"] == 5
    assert summary["latency_ms_p50"] >= 500
    assert summary["latency_ms_p95"] >= 1600
    assert summary["backends"] == {"groq": 5}
    assert summary["filter_hits_total"] == 1


def test_summary_empty(tmp_path: Path):
    store = MetricsStore(tmp_path)
    summary = store.session_summary(persona="renee")
    assert summary == {"turns": 0}


def test_sycophancy_counted(tmp_path: Path):
    store = MetricsStore(tmp_path)
    t0 = time.time()
    store.record_turn(_mk(t0, syc=True))
    store.record_turn(_mk(t0 + 1, syc=False))
    summary = store.session_summary(persona="renee", since_ts=t0 - 1)
    assert summary["sycophancy_hits"] == 1
