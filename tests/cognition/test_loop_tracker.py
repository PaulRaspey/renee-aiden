"""Unit tests for LoopTracker."""
from __future__ import annotations

import pytest

from src.cognition.loop_tracker import LoopTracker
from src.cognition.fringe import Turn


def test_defer_marker_creates_loop():
    lt = LoopTracker()
    t = Turn(user="ok", assistant="let me think about that and i'll come back to it")
    out = lt.check(t, turn_count=1)
    assert out is not None
    assert out.salience == 1.0
    assert out.last_touched_turn == 1
    assert out.summary  # non-empty


def test_explicit_unanswered_question_creates_loop():
    lt = LoopTracker()
    t = Turn(user="hey", assistant="what would you do in that situation?")
    out = lt.check(t, turn_count=2)
    assert out is not None
    assert 0.5 <= out.salience <= 1.0


def test_self_answered_question_does_not_create_loop():
    lt = LoopTracker()
    # Question followed by long declarative answer should NOT register.
    t = Turn(
        user="hey",
        assistant="what would you do in that situation? well i think i would just stay calm and try to figure out what was going on before doing anything rash",
    )
    out = lt.check(t, turn_count=3)
    assert out is None


def test_pure_statement_creates_no_loop():
    lt = LoopTracker()
    t = Turn(user="i agree", assistant="yes that is correct")
    out = lt.check(t, turn_count=4)
    assert out is None


def test_topic_shift_marker_creates_weak_loop():
    lt = LoopTracker()
    t = Turn(user="ok", assistant="anyway, different topic — let's talk about something else.")
    out = lt.check(t, turn_count=5)
    assert out is not None
    assert out.salience < 1.0  # shifts are weaker than explicit defers
    assert "shifted" in out.summary.lower() or out.summary  # readable summary


def test_user_side_question_also_counts():
    lt = LoopTracker()
    # No assistant text; user raises a question.
    t = Turn(user="what do you think about that?", assistant="")
    out = lt.check(t, turn_count=6)
    assert out is not None


def test_empty_turn_returns_none():
    lt = LoopTracker()
    out = lt.check(Turn(user="", assistant=""), turn_count=7)
    assert out is None
