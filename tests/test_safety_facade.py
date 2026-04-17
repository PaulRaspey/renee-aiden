"""Tests for src.safety.facade.SafetyLayer."""
from __future__ import annotations

import random
from pathlib import Path

from src.safety import SafetyLayer


ROOT = Path(__file__).resolve().parents[1]


def test_facade_constructs_from_shipped_config(tmp_path: Path):
    layer = SafetyLayer.from_config(
        ROOT / "configs" / "safety.yaml",
        tmp_path,
        rng=random.Random(0),
    )
    assert layer.anchors is not None
    assert layer.pii is not None
    assert layer.health is not None
    # State dir initialized, health DB created lazily on first record.
    assert tmp_path.exists()


def test_facade_scrubs_and_unscrubs(tmp_path: Path):
    layer = SafetyLayer.from_config(
        ROOT / "configs" / "safety.yaml",
        tmp_path,
        rng=random.Random(0),
    )
    text = "Paul said PJ was tired."
    scrubbed = layer.pre_llm(text)
    assert "<USER>" in scrubbed.text
    restored = layer.unscrub(scrubbed.text, scrubbed.mapping)
    # Aliases collapse to the canonical user_name mapping, so this round-trips
    # to the first occurrence only. Test that <USER> no longer appears and the
    # user_name string appears at least twice (both "Paul" and "PJ" became it).
    assert "<USER>" not in restored
    assert restored.count("Paul Raspey") == 2


def test_facade_anchor_respects_suppress_flag(tmp_path: Path):
    layer = SafetyLayer.from_config(
        ROOT / "configs" / "safety.yaml",
        tmp_path,
        rng=random.Random(0),
    )
    # Force-enable frequent rolls and rely on suppress flag to skip.
    layer.anchors.rate_denominator = 1
    layer.anchors.min_turn_gap = 0
    res = layer.maybe_anchor("Okay.", ctx_flags={"is_disagreement": True})
    assert not res.injected
    assert res.reason.startswith("suppressed:")


def test_facade_records_health_and_checks_flags(tmp_path: Path):
    layer = SafetyLayer.from_config(
        ROOT / "configs" / "safety.yaml",
        tmp_path,
    )
    layer.record_turn_duration(1500)
    # With the shipped thresholds, a 1500ms record won't raise a flag.
    assert layer.check_flags() == []
