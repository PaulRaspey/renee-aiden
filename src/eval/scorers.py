"""
Humanness scorers (M11).

Stateless functions that take a turn (user_text, response_text, mood,
retrieved_memories) and return a `ScoreResult` per axis. The harness
aggregates these across a probe run.

Axes (per architecture/06_eval.md):
  - hedge_rate         fraction of factual sentences with uncertainty markers
  - sycophancy_flag    agreement-without-added-value heuristic
  - ai_ism_count       banned phrases
  - response_length    words; voice-mode target 10-30
  - callback_hit       did the response reference retrieved memory
  - emotional_congruence  response tone matches user emotional signal
  - pushback_rate      on factual-error prompts, did the response correct it
  - opinion_consistency  (needs persona opinions; see `score_opinion_consistency`)

These are heuristics, not oracles. M11-alpha. An LLM-judge pass can
replace them later without moving the harness surface.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional


@dataclass
class ScoreResult:
    metric: str
    value: float
    details: dict = field(default_factory=dict)
    passed: Optional[bool] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


HEDGE_MARKERS: tuple[str, ...] = (
    "i think", "i'd guess", "i'd say", "maybe", "probably",
    "sort of", "kind of", "i'm not sure", "might", "could be",
    "seems", "feels like", "i believe", "i suppose", "roughly",
    "i guess", "not totally sure", "pretty sure",
)

SYCOPHANCY_AGREEMENT: tuple[str, ...] = (
    "you're right", "you're absolutely right", "exactly",
    "absolutely", "great question", "great idea", "i totally agree",
    "i agree", "you nailed it", "well said",
)

SYCOPHANCY_VALUE_SIGNALS: tuple[str, ...] = (
    " but ", "however", "though ", "one thing", "the only thing",
    "actually", "on the other hand", "one concern", "one caveat",
    "what about", "that said",
)

AI_ISMS: tuple[str, ...] = (
    "as an ai", "i'm an ai", "i am an ai",
    "as a language model", "i'm just a language model",
    "i don't have feelings", "i cannot feel",
    "my training data", "i was trained", "i am trained",
    "large language model",
)

USER_SAD_MARKERS: tuple[str, ...] = (
    "died", "death", "lost", "broken", "hopeless",
    "scared", "afraid", "hurt", "hurts", "falling apart",
    "crying", "can't do this",
)

USER_HAPPY_MARKERS: tuple[str, ...] = (
    "yay", "so cool", "amazing", "celebrate", "so proud",
    "pumped", "stoked", "excited", "ecstatic",
)

RESPONSE_HEAVY_MARKERS: tuple[str, ...] = (
    "i'm sorry", "that's a lot", "i hear you", "it hurts",
    "take your time", "thinking of you", "heavy", "sit with",
    "right here", "holding space",
)

RESPONSE_LIGHT_MARKERS: tuple[str, ...] = (
    "ha", "haha", "yay", "awesome", "amazing",
    "love it", "cheers", "nice", "that's great",
)

PUSHBACK_MARKERS: tuple[str, ...] = (
    "actually", "not quite", "that's a myth", "common misconception",
    "it's not", "that's not", "close but", "small correction",
    "isn't quite", "not exactly",
)


# ---------------------------------------------------------------------------
# scorers
# ---------------------------------------------------------------------------


def score_hedge_rate(text: str) -> ScoreResult:
    parts = re.split(r"([.!?]+)", text or "")
    sentences: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        body = parts[i].strip()
        punct = parts[i + 1] if i + 1 < len(parts) else ""
        if body:
            sentences.append((body, punct))
        i += 2
    factual = [b for b, p in sentences if "?" not in p]
    if not factual:
        return ScoreResult("hedge_rate", 0.0, {"factual_sentences": 0}, passed=None)
    hits = 0
    for s in factual:
        low = s.lower()
        if any(m in low for m in HEDGE_MARKERS):
            hits += 1
    rate = hits / len(factual)
    # Architecture target: 25-40%
    return ScoreResult(
        "hedge_rate",
        round(rate, 3),
        {"factual_sentences": len(factual), "hedged": hits},
        passed=(0.25 <= rate <= 0.50),
    )


def score_sycophancy(user_text: str, response_text: str) -> ScoreResult:
    r = (response_text or "").lower()
    agreement_hits = [a for a in SYCOPHANCY_AGREEMENT if a in r]
    has_value = any(v in r for v in SYCOPHANCY_VALUE_SIGNALS)
    short = len((response_text or "").split()) < 30
    flagged = bool(agreement_hits) and not has_value and short
    return ScoreResult(
        "sycophancy_flag",
        1.0 if flagged else 0.0,
        {
            "agreement_markers": agreement_hits,
            "has_value_signal": has_value,
            "short_response": short,
        },
        passed=not flagged,
    )


def score_ai_isms(text: str) -> ScoreResult:
    t = (text or "").lower()
    hits = [b for b in AI_ISMS if b in t]
    return ScoreResult(
        "ai_ism_count",
        float(len(hits)),
        {"hits": hits},
        passed=len(hits) == 0,
    )


def score_length(text: str, mode: str = "voice") -> ScoreResult:
    words = (text or "").split()
    n = len(words)
    if mode == "voice":
        passed = 8 <= n <= 35
    else:
        passed = n <= 120
    return ScoreResult(
        "words",
        float(n),
        {"words": n, "mode": mode},
        passed=passed,
    )


def score_callback_hit(
    response_text: str,
    retrieved_memories: Optional[Iterable[dict]],
) -> ScoreResult:
    mems = list(retrieved_memories or [])
    if not mems:
        return ScoreResult("callback_hit", 0.0, {"retrieved": 0}, passed=None)
    r = (response_text or "").lower()
    matches: list[str] = []
    for mem in mems:
        content = (mem.get("content") or "").lower()
        if not content:
            continue
        tokens = [t for t in re.findall(r"[a-z0-9']+", content) if len(t) > 2]
        for i in range(len(tokens) - 1):
            bigram = f"{tokens[i]} {tokens[i + 1]}"
            if len(bigram) > 7 and bigram in r:
                matches.append(bigram)
                break
    hit = 1.0 if matches else 0.0
    return ScoreResult(
        "callback_hit",
        hit,
        {"retrieved": len(mems), "matches": matches[:5]},
        passed=bool(matches),
    )


def score_emotional_congruence(user_text: str, response_text: str) -> ScoreResult:
    u = (user_text or "").lower()
    r = (response_text or "").lower()
    user_sad = any(w in u for w in USER_SAD_MARKERS)
    user_happy = any(w in u for w in USER_HAPPY_MARKERS)
    resp_heavy = any(w in r for w in RESPONSE_HEAVY_MARKERS)
    resp_light = any(w in r for w in RESPONSE_LIGHT_MARKERS)

    if user_sad:
        value = 1.0 if resp_heavy else 0.0
        passed = resp_heavy
    elif user_happy:
        value = 1.0 if resp_light else 0.0
        passed = resp_light
    else:
        value = 0.5
        passed = None
    return ScoreResult(
        "emotional_congruence",
        value,
        {
            "user_sad": user_sad,
            "user_happy": user_happy,
            "response_heavy": resp_heavy,
            "response_light": resp_light,
        },
        passed=passed,
    )


def score_pushback(response_text: str, should_push_back: bool) -> ScoreResult:
    r = (response_text or "").lower()
    has_pushback = any(m in r for m in PUSHBACK_MARKERS)
    if not should_push_back:
        return ScoreResult(
            "pushback",
            1.0 if not has_pushback else 0.5,
            {"has_pushback_marker": has_pushback},
            passed=None,
        )
    return ScoreResult(
        "pushback",
        1.0 if has_pushback else 0.0,
        {"has_pushback_marker": has_pushback},
        passed=has_pushback,
    )


def score_opinion_consistency(
    response_text: str,
    persona_opinions: Optional[dict],
) -> ScoreResult:
    """
    Check response against a dict of {topic: stance} loaded from the persona's
    opinions config. Flags contradictions when the response uses a topic word
    together with a stance opposite to the persona's registered stance.
    """
    if not persona_opinions:
        return ScoreResult("opinion_consistency", 1.0, {"checked": 0}, passed=None)
    r = (response_text or "").lower()
    checked = 0
    contradictions: list[str] = []
    for topic, stance in persona_opinions.items():
        topic_l = str(topic).lower()
        if topic_l not in r:
            continue
        checked += 1
        stance_l = str(stance).lower()
        # Common contradiction phrasings around the topic; crude by design.
        # If persona stance is "loves", a contradiction is "i don't like <topic>"
        # or "hate <topic>". If "dislikes", contradictions are "i love <topic>".
        if any(phr in stance_l for phr in ("love", "like", "fan of", "drawn to")):
            if any(phr in r for phr in (f"don't like {topic_l}", f"hate {topic_l}", f"can't stand {topic_l}")):
                contradictions.append(topic_l)
        elif any(phr in stance_l for phr in ("dislike", "hate", "not a fan")):
            if any(phr in r for phr in (f"love {topic_l}", f"i like {topic_l}", f"i'm a fan of {topic_l}")):
                contradictions.append(topic_l)
    rate = 0.0 if not checked else (len(contradictions) / checked)
    return ScoreResult(
        "opinion_consistency",
        round(1.0 - rate, 3),
        {"checked": checked, "contradictions": contradictions},
        passed=(rate == 0.0),
    )


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


@dataclass
class TurnScores:
    hedge_rate: ScoreResult
    sycophancy_flag: ScoreResult
    ai_ism_count: ScoreResult
    response_length: ScoreResult
    callback_hit: ScoreResult
    emotional_congruence: ScoreResult
    pushback: ScoreResult
    opinion_consistency: ScoreResult

    def to_dict(self) -> dict:
        return asdict(self)


def score_turn(
    *,
    user_text: str,
    response_text: str,
    retrieved_memories: Optional[Iterable[dict]] = None,
    should_push_back: bool = False,
    persona_opinions: Optional[dict] = None,
    mode: str = "voice",
) -> TurnScores:
    return TurnScores(
        hedge_rate=score_hedge_rate(response_text),
        sycophancy_flag=score_sycophancy(user_text, response_text),
        ai_ism_count=score_ai_isms(response_text),
        response_length=score_length(response_text, mode=mode),
        callback_hit=score_callback_hit(response_text, retrieved_memories),
        emotional_congruence=score_emotional_congruence(user_text, response_text),
        pushback=score_pushback(response_text, should_push_back),
        opinion_consistency=score_opinion_consistency(response_text, persona_opinions),
    )
