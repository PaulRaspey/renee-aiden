"""Unit tests for src.eval.ab (A/B queue)."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from src.eval.ab import ABPair, ABQueue


@pytest.fixture
def queue(tmp_path: Path) -> ABQueue:
    return ABQueue(tmp_path / "ab.db", rng=random.Random(0))


def test_queue_pair_and_fetch_next(queue: ABQueue):
    p = queue.queue_pair(prompt="Hey", candidate="cand", baseline="base")
    assert isinstance(p, ABPair)
    n = queue.next_pair()
    assert n is not None
    assert n.pair_id == p.pair_id
    assert {n.label_a, n.label_b} == {"candidate", "baseline"}


def test_random_swap_hides_label(tmp_path: Path):
    # Flip between seeds; both labels should end up on 'a' across trials.
    out_a = {"candidate": 0, "baseline": 0}
    for seed in range(50):
        q = ABQueue(tmp_path / f"ab_{seed}.db", rng=random.Random(seed))
        p = q.queue_pair(prompt="x", candidate="c", baseline="b")
        out_a[p.label_a] += 1
    assert out_a["candidate"] > 5
    assert out_a["baseline"] > 5


def test_record_rating_and_win_rate(queue: ABQueue):
    # Queue two pairs; rate both in favor of candidate by tracking its side.
    p1 = queue.queue_pair(prompt="p1", candidate="c1", baseline="b1")
    p2 = queue.queue_pair(prompt="p2", candidate="c2", baseline="b2")
    cand_chosen = lambda p: "a" if p.label_a == "candidate" else "b"
    queue.record_rating(p1.pair_id, cand_chosen(p1), margin=5)
    queue.record_rating(p2.pair_id, cand_chosen(p2), margin=4)
    result = queue.win_rate("candidate")
    assert result["ratings"] == 2
    assert result["wins"] == 2
    assert result["win_rate"] == 1.0
    assert result["margin_sum"] == 9


def test_pending_count_excludes_rated(queue: ABQueue):
    p = queue.queue_pair(prompt="p", candidate="c", baseline="b")
    assert queue.pending_count() == 1
    queue.record_rating(p.pair_id, "a")
    assert queue.pending_count() == 0


def test_invalid_choice_raises(queue: ABQueue):
    p = queue.queue_pair(prompt="p", candidate="c", baseline="b")
    with pytest.raises(ValueError):
        queue.record_rating(p.pair_id, "neither")


def test_margin_clamped(queue: ABQueue):
    p = queue.queue_pair(prompt="p", candidate="c", baseline="b")
    queue.record_rating(p.pair_id, "a", margin=99)
    p2 = queue.queue_pair(prompt="p2", candidate="c2", baseline="b2")
    queue.record_rating(p2.pair_id, "a", margin=-1)
    # should be clamped to [1,5]
    stats = queue.win_rate("candidate")
    assert 0 <= stats["margin_sum"] <= 10
