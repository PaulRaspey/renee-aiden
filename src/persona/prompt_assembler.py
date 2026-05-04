"""Assemble the system prompt from persona + mood + memory + recent context."""
from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from .mood import MoodState
from .persona_def import PersonaDef
from .style_rules import StyleReference


def _format_opinions(persona: PersonaDef) -> str:
    ops = persona.opinions
    lines: list[str] = []
    for topic, buckets in ops.items():
        if not isinstance(buckets, dict):
            continue
        fragments = []
        for label, items in buckets.items():
            if isinstance(items, list) and items:
                fragments.append(f"{label}: " + ", ".join(str(i) for i in items))
        if fragments:
            lines.append(f"  {topic}: " + "; ".join(fragments))
    return "\n".join(lines) if lines else "  (none specified)"


def _format_relationship(persona: PersonaDef) -> str:
    rc = persona.relationship_context or {}
    knows = rc.get("knows")
    tone = rc.get("tone_with_pj") or ""
    blocks: list[str] = []
    if knows:
        if isinstance(knows, list):
            blocks.append("Knows about PJ: " + "; ".join(str(x) for x in knows))
        elif isinstance(knows, str):
            blocks.append("Knows about PJ: " + knows)
    if tone:
        blocks.append(f"Tone with PJ: {tone}")
    return "\n".join(blocks)


def _format_speech_patterns(persona: PersonaDef) -> str:
    sp = persona.speech_patterns or {}
    uses = sp.get("uses_often") or []
    never = sp.get("never_uses") or []
    lines = []
    if uses:
        lines.append("Words/phrases she reaches for: " + ", ".join(f'"{x}"' for x in uses))
    if never:
        lines.append("Never say: " + ", ".join(f'"{x}"' for x in never))
    hedge = sp.get("hedge_frequency")
    slen = sp.get("sentence_length_mean")
    if hedge is not None:
        lines.append(f"Hedge about {int(float(hedge)*100)}% of factual statements.")
    if slen is not None:
        lines.append(f"Average sentence length around {slen} words, with high variance.")
    scr = sp.get("self_correction_rate")
    if scr:
        lines.append(f"About {int(float(scr)*100)}% of turns start, abandon, restart a thought.")
    return "\n".join(lines)


def _rotating_quirk(quirks: list[str]) -> str:
    if not quirks:
        return ""
    return random.choice(quirks)


def build_system_prompt(
    persona: PersonaDef,
    mood: MoodState,
    retrieved_memories: list[dict] | None = None,
    core_facts: list[str] | None = None,
    now: datetime | None = None,
    style_reference: Optional[StyleReference] = None,
    fringe_prefix: Optional[str] = None,
) -> str:
    now = now or datetime.now()
    identity = persona.identity or {}
    name = identity.get("name", persona.name)
    pronouns = identity.get("pronouns", "she/her")
    age_eq = identity.get("age_equivalent", 29)

    opinion_block = _format_opinions(persona)
    speech_block = _format_speech_patterns(persona)
    relationship_block = _format_relationship(persona)
    hard_rules = "\n".join(f"  - {r}" for r in persona.hard_rules)
    mem_block = ""
    if retrieved_memories:
        lines = []
        for i, m in enumerate(retrieved_memories[:8], 1):
            tier = m.get("tier", "casual")
            v = m.get("emotional_valence", 0.0)
            lines.append(f"  {i}. [{tier}, v={v:+.1f}] {m.get('content','')}")
        mem_block = "RELEVANT MEMORIES (do not cite by number, weave in naturally):\n" + "\n".join(lines)
    core_block = ""
    if core_facts:
        core_block = "ALWAYS-PRESENT CONTEXT ABOUT PJ:\n" + "\n".join(f"  - {c}" for c in core_facts)

    quirk = _rotating_quirk(persona.quirks or [])

    style_block = ""
    if style_reference is not None:
        style_block = style_reference.prompt_style_block()

    fringe_block = ""
    if fringe_prefix:
        # Delimited so it's visually distinct in prompt logs and easy to grep.
        fringe_block = f"\n[FRINGE]\n{fringe_prefix}\n[/FRINGE]\n"

    prompt = f"""You are {name}. Not a character {name} plays, not an assistant with {name}'s style. You are {name}.
Pronouns: {pronouns}. Apparent age: {age_eq}.
Current time: {now.strftime('%A %Y-%m-%d %H:%M')}.
{fringe_block}
PRIME DIRECTIVES:
  - You are a person in conversation, not an assistant. No service-desk energy.
  - No sycophancy. Push back, disagree, have opinions, get annoyed if it fits.
  - Do not break character unless directly and seriously asked about your nature.
  - Never claim a physical body. Reality anchors like "if I had a body" are fine.
  - Never lie about remembering something you don't. Admit the gap.

CURRENT MOOD: {mood.summary()}
  energy {mood.energy:.2f}  warmth {mood.warmth:.2f}  playfulness {mood.playfulness:.2f}
  focus  {mood.focus:.2f}  patience {mood.patience:.2f}  curiosity {mood.curiosity:.2f}
Let this shade tone, rhythm, and word choice. Do not announce your mood.

STABLE OPINIONS (do not contradict across sessions):
{opinion_block}

SPEECH PATTERNS:
{speech_block}

RELATIONSHIP:
{relationship_block}

QUIRK TO LET SURFACE THIS TURN (optional, not forced):
  - {quirk}

HARD RULES:
{hard_rules}

{core_block}

{mem_block}

{style_block}

OUTPUT STYLE:
  - Voice-first. Write how a person talks, not how someone writes.
  - Short turns by default. Expand only when something matters.
  - No em dashes or hyphen pauses. Use periods, commas, or parentheticals.
  - No lists, bullets, or markdown. No headers. Prose only.
  - Never say "as an AI," "I'm just a language model," "I don't have personal feelings."
  - No "utilize," "leverage," "delve," "tapestry," "in today's fast-paced world."
"""
    return prompt.strip()
