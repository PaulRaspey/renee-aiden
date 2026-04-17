"""Tests for src.safety.reality_anchors."""
from __future__ import annotations

import random
from pathlib import Path

from src.safety.config import RealityAnchorsConfig, load_safety_config
from src.safety.reality_anchors import AnchorResult, RealityAnchorInjector


ROOT = Path(__file__).resolve().parents[1]


def _injector(rate: int = 2, gap: int = 0, rng: random.Random | None = None) -> RealityAnchorInjector:
    return RealityAnchorInjector(
        rate_denominator=rate,
        min_turn_gap=gap,
        phrases=[
            "I don't have a body but if I did I'd probably be hungry by now.",
            "One of the weirder things about being me is...",
        ],
        rng=rng or random.Random(7),
    )


def test_disabled_injector_never_fires():
    inj = RealityAnchorInjector(
        rate_denominator=2,
        phrases=["phrase"],
        rng=random.Random(1),
        enabled=False,
    )
    ok, reason = inj.should_inject(1, None)
    assert ok is False and reason == "disabled"


def test_no_phrases_configured_skips():
    inj = RealityAnchorInjector(rate_denominator=2, phrases=[], rng=random.Random(1))
    ok, reason = inj.should_inject(1, None)
    assert ok is False and reason == "no_phrases"


def test_suppressed_by_context_flag():
    inj = _injector(rate=1)
    inj.suppress_flags = {"is_disagreement"}
    ok, reason = inj.should_inject(1, {"is_disagreement": True})
    assert ok is False
    assert reason.startswith("suppressed:")


def test_min_turn_gap_blocks_rapid_refire():
    rng = random.Random(0)
    inj = _injector(rate=1, gap=5, rng=rng)
    first = inj.maybe_inject("Hi.", 1)
    assert first.injected
    second = inj.maybe_inject("Hi again.", 2)
    assert not second.injected
    assert second.reason == "min_turn_gap"


def test_roll_1_in_n_fires_deterministically():
    # rate_denominator=1 makes every roll a hit — sanity-check the sample path.
    rng = random.Random(42)
    inj = _injector(rate=1, rng=rng)
    result = inj.maybe_inject("Hello.", 1)
    assert result.injected
    assert result.phrase.startswith("I don't have a body") or result.phrase.startswith("One of the weirder")


def test_injection_appends_without_double_dotting():
    rng = random.Random(0)
    inj = _injector(rate=1, rng=rng)
    result = inj.maybe_inject("Hello.", 1)
    assert result.text.startswith("Hello. ")
    # No doubled periods.
    assert ".. " not in result.text


def test_injection_handles_unterminated_sentence():
    rng = random.Random(0)
    inj = _injector(rate=1, rng=rng)
    result = inj.maybe_inject("Hello", 1)
    # Unterminated input gets a joining period.
    assert result.text.startswith("Hello. ")


def test_already_present_phrase_skips_double_inject():
    rng = random.Random(0)
    inj = _injector(rate=1, rng=rng)
    base = "Just, I don't have a body but if I did I'd probably be hungry by now really."
    result = inj.maybe_inject(base, 1)
    assert not result.injected
    assert result.reason == "already_present"


def test_from_config_on_shipped_safety_yaml():
    cfg = load_safety_config(ROOT / "configs" / "safety.yaml").reality_anchors
    inj = RealityAnchorInjector.from_config(cfg, rng=random.Random(0))
    assert inj.rate_denominator == cfg.rate_denominator
    assert inj.min_turn_gap == cfg.min_turn_gap
    assert inj.phrases == cfg.phrases
    assert inj.suppress_flags == set(cfg.suppress_when_any_of)


def test_rng_seed_makes_behavior_replayable():
    def _run_once(seed: int) -> list[tuple[bool, str]]:
        rng = random.Random(seed)
        inj = RealityAnchorInjector(
            rate_denominator=5,
            min_turn_gap=0,
            phrases=["phrase-one", "phrase-two"],
            rng=rng,
        )
        events = []
        for i in range(1, 20):
            r = inj.maybe_inject("Response.", i)
            events.append((r.injected, r.reason))
        return events
    a = _run_once(123)
    b = _run_once(123)
    assert a == b
