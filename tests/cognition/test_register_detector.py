"""Unit tests for RegisterDetector."""
from __future__ import annotations

import numpy as np
import pytest

from src.cognition.register_detector import DIMS, RegisterDetector
from src.cognition.fringe import Turn


def _idx(name: str) -> int:
    return DIMS.index(name)


def test_simplex_sums_to_one():
    rd = RegisterDetector()
    out = rd.detect(Turn(user="hello", assistant="hi"))
    assert out.shape == (3,)
    assert pytest.approx(float(out.sum()), abs=1e-5) == 1.0
    assert np.all(out >= 0.0)


def test_technical_dominates_on_code_and_jargon():
    rd = RegisterDetector()
    out = rd.detect(Turn(
        user="the function reads from the api endpoint, deploy via ssh",
        assistant="```python\nresult = json.dumps(payload)\n```\nthe schema validates that.",
    ))
    assert int(np.argmax(out)) == _idx("technical")


def test_intimate_dominates_on_emotional_disclosure():
    rd = RegisterDetector()
    out = rd.detect(Turn(
        user="i feel scared and lonely, i miss what we had",
        assistant="i hear you. i love that you trust me with this. i feel it too.",
    ))
    assert int(np.argmax(out)) == _idx("intimate")


def test_playful_dominates_on_humor_markers():
    rd = RegisterDetector()
    out = rd.detect(Turn(
        user="lol that's ridiculous! imagine that!",
        assistant="haha! absolutely absurd! lmao what if we did it anyway!",
    ))
    assert int(np.argmax(out)) == _idx("playful")


def test_empty_turn_yields_uniform_simplex():
    rd = RegisterDetector()
    out = rd.detect(Turn(user="", assistant=""))
    assert pytest.approx(float(out.sum()), abs=1e-5) == 1.0
    # Uniform-ish: max - min is small
    assert float(out.max() - out.min()) < 1e-5


def test_assistant_weight_higher_than_user():
    """Same content on each side should produce a stronger signal when on
    assistant than on user, because assistant_weight (0.6) > user_weight (0.4)."""
    rd = RegisterDetector()
    intimate_text = "i feel scared and i miss you and i love this"
    neutral_text = "okay then"
    on_assistant = rd.detect(Turn(user=neutral_text, assistant=intimate_text))
    on_user = rd.detect(Turn(user=intimate_text, assistant=neutral_text))
    intimate_idx = _idx("intimate")
    assert on_assistant[intimate_idx] > on_user[intimate_idx]
