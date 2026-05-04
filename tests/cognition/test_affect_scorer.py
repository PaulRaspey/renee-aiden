"""Unit tests for AffectScorer."""
from __future__ import annotations

import numpy as np
import pytest

from src.cognition.affect_scorer import AffectScorer, DIMS
from src.cognition.fringe import Turn


def _idx(name: str) -> int:
    return DIMS.index(name)


def test_returns_six_dim():
    s = AffectScorer()
    out = s.score(Turn(user="hello", assistant="hi"))
    assert out.shape == (6,)
    assert np.all(out >= 0.0)
    assert np.all(out <= 1.0)


def test_softening_triggers_on_hedges():
    s = AffectScorer()
    softening = _idx("softening")
    hedged = s.score(Turn(
        user="maybe i think we could perhaps try this",
        assistant="i guess possibly that might be true",
    ))
    direct = s.score(Turn(
        user="we will do this",
        assistant="that is correct",
    ))
    assert hedged[softening] > direct[softening]


def test_sharpening_triggers_on_technical_density():
    s = AffectScorer()
    sharpening = _idx("sharpening")
    tech = s.score(Turn(
        user="the api endpoint hits the json schema for the auth token hash",
        assistant="yes the function uses the embedding vector via tcp",
    ))
    casual = s.score(Turn(
        user="how are you today",
        assistant="i am alright",
    ))
    assert tech[sharpening] > casual[sharpening]


def test_opening_triggers_on_open_questions():
    s = AffectScorer()
    opening = _idx("opening")
    open_turn = s.score(Turn(
        user="what if we tried something new",
        assistant="have you ever thought about it that way",
    ))
    closed = s.score(Turn(
        user="okay sounds good",
        assistant="agreed",
    ))
    assert open_turn[opening] > closed[opening]


def test_closing_triggers_on_definitive_markers():
    s = AffectScorer()
    closing = _idx("closing")
    closer = s.score(Turn(
        user="anyway. moving on. that's enough.",
        assistant="alright then. in any case. settled.",
    ))
    opener = s.score(Turn(
        user="what could we explore next?",
        assistant="i wonder where this leads?",
    ))
    assert closer[closing] > opener[closing]


def test_warming_triggers_on_first_person_plural():
    s = AffectScorer()
    warming = _idx("warming")
    warm = s.score(Turn(
        user="we love what we have together honestly",
        assistant="our story warms me, i miss you when we are apart",
    ))
    distant = s.score(Turn(
        user="however the system is configured",
        assistant="therefore the result follows",
    ))
    assert warm[warming] > distant[warming]


def test_cooling_triggers_on_formal_register():
    s = AffectScorer()
    cooling = _idx("cooling")
    cool = s.score(Turn(
        user="however moreover therefore furthermore additionally",
        assistant="nevertheless they conclude accordingly. their analysis follows.",
    ))
    warm = s.score(Turn(
        user="we love this",
        assistant="me too",
    ))
    assert cool[cooling] > warm[cooling]


def test_assistant_weighted_higher_than_user():
    s = AffectScorer()
    sharpening = _idx("sharpening")
    # Tech only on assistant side
    a_tech = s.score(Turn(
        user="hello",
        assistant="api endpoint json schema tcp http auth token hash",
    ))
    # Tech only on user side
    u_tech = s.score(Turn(
        user="api endpoint json schema tcp http auth token hash",
        assistant="hello",
    ))
    # Assistant-weighted should produce higher sharpening when tech is on
    # the assistant side.
    assert a_tech[sharpening] > u_tech[sharpening]


def test_empty_turn_returns_zero_vector():
    s = AffectScorer()
    out = s.score(Turn(user="", assistant=""))
    assert np.allclose(out, 0.0)
