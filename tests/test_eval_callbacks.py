"""Unit tests for src.eval.callbacks."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.eval.callbacks import CallbackTracker


@pytest.fixture
def tracker(tmp_path: Path) -> CallbackTracker:
    return CallbackTracker(tmp_path / "callbacks.db")


def test_no_memories_returns_no_event(tracker: CallbackTracker):
    ev = tracker.log_turn("turn-1", "response text", [])
    assert ev is None


def test_logs_hit_when_response_references_memory(tracker: CallbackTracker):
    memories = [{"content": "Paul mentioned learning to play guitar"}]
    ev = tracker.log_turn("turn-2", "You could play guitar this weekend.", memories)
    assert ev is not None
    assert ev.hit is True
    assert ev.retrieved_count == 1


def test_logs_miss_when_response_does_not_reference(tracker: CallbackTracker):
    memories = [{"content": "Paul was reading about gardening"}]
    ev = tracker.log_turn("turn-3", "How was your afternoon?", memories)
    assert ev is not None
    assert ev.hit is False


def test_accuracy_computes_ratio(tracker: CallbackTracker):
    tracker.log_turn(
        "t1",
        "Let's book the guitar lessons on Saturday.",
        [{"content": "paul wants guitar lessons"}],
    )
    tracker.log_turn(
        "t2",
        "different topic entirely",
        [{"content": "marcus the student was unusually quiet"}],
    )
    stats = tracker.accuracy()
    assert stats["opportunities"] == 2
    assert stats["hits"] == 1
    assert stats["accuracy"] == 0.5


def test_accuracy_empty_returns_zero(tracker: CallbackTracker):
    stats = tracker.accuracy()
    assert stats == {"opportunities": 0, "hits": 0, "accuracy": 0.0}
