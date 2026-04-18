"""Output filters applied post-LLM: AI-isms, em-dash replacer, hedge enforcement,
sycophancy detection, length governor.

All filters are pure text transforms. Regeneration hints are returned alongside
the filtered text; the persona core decides whether to regenerate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .persona_def import PersonaDef

AI_ISMS = [
    r"\bas an ai\b",
    r"\bas a language model\b",
    r"\bi am an ai\b",
    r"\bi'?m (?:just |only )?(?:a |an )?(?:language model|large language model|ai assistant)\b",
    r"\bi(?: do not|'?m not| don'?t) have (?:personal )?(?:feelings|emotions|a body)\b",
    r"\bi cannot provide\b",
    r"\bi can'?t provide\b",
    r"\bas of my (?:last )?(?:knowledge|training) (?:cutoff|update)\b",
]
AI_ISMS_RE = re.compile("|".join(AI_ISMS), re.IGNORECASE)

SLOP_WORDS = [
    r"\butilize\b",
    r"\bleverage\b",
    r"\bdelve\b",
    r"\btapestry\b",
    r"\bin today'?s fast-paced world\b",
    r"\bnavigate the complexities\b",
    r"\brealm of\b",
]
SLOP_WORDS_RE = re.compile("|".join(SLOP_WORDS), re.IGNORECASE)

SLOP_REPLACEMENTS = {
    "utilize": "use",
    "leverage": "use",
    "delve": "get into",
    "tapestry": "mess",
}

HEDGE_MARKERS = [
    "i think", "maybe", "probably", "kind of", "sort of", "i'm not sure",
    "could be", "might", "seems like", "it feels like", "honestly though",
    "i don't know", "i could be wrong", "i think so",
]

AGREEMENT_MARKERS = [
    "you're right", "absolutely", "great point", "good point", "exactly",
    "i totally agree", "completely agree", "i agree", "that's perfect",
    "wonderful", "amazing", "brilliant",
]


@dataclass
class FilterReport:
    text: str
    hits: list[str] = field(default_factory=list)
    regenerate_hint: str | None = None
    sycophancy_flag: bool = False

    def hit_rate(self, n_turns: int) -> float:
        """Hits per N turns. Stateless — caller supplies the turn count."""
        if n_turns <= 0:
            return 0.0
        return len(self.hits) / n_turns


def strip_ai_isms(text: str) -> tuple[str, list[str]]:
    hits = AI_ISMS_RE.findall(text)
    cleaned = AI_ISMS_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+([,.!?])", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned).strip()
    return cleaned, [h if isinstance(h, str) else "match" for h in hits]


def replace_slop(text: str) -> tuple[str, list[str]]:
    hits: list[str] = []
    def repl(m: re.Match) -> str:
        word = m.group(0).lower()
        hits.append(word)
        return SLOP_REPLACEMENTS.get(word.split()[0], "")
    new = SLOP_WORDS_RE.sub(repl, text)
    return new, hits


def replace_em_dashes(text: str) -> tuple[str, int]:
    new = text.replace("—", ", ")
    new = re.sub(r"(\w)\s--\s(\w)", r"\1, \2", new)
    new = re.sub(r"(\w)\s-\s(\w)", r"\1, \2", new)
    count = text.count("—") + len(re.findall(r"\w\s--\s\w|\w\s-\s\w", text))
    new = re.sub(r",\s*,", ",", new)
    return new, count


def remove_markdown(text: str) -> str:
    new = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    new = re.sub(r"^#{1,6}\s+", "", new, flags=re.MULTILINE)
    new = re.sub(r"\*\*(.+?)\*\*", r"\1", new)
    new = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", new)
    return new


def count_hedges(text: str) -> int:
    lower = text.lower()
    return sum(lower.count(m) for m in HEDGE_MARKERS)


def detect_sycophancy(text: str) -> bool:
    lower = text.lower().strip()
    if not lower:
        return False
    first_120 = lower[:120]
    hits = sum(1 for m in AGREEMENT_MARKERS if m in first_120)
    pushback = any(p in lower for p in ["but ", "though ", "wait", "actually", "i don't", "i'm not sure", "no,"])
    return hits >= 2 and not pushback


def count_factual_claims(text: str) -> int:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    # crude: non-question, non-exclamation sentences that aren't clearly subjective
    count = 0
    for s in sentences:
        s_strip = s.strip()
        if not s_strip or s_strip.endswith("?"):
            continue
        lower = s_strip.lower()
        if any(lower.startswith(p) for p in ("i feel", "i think i", "i kind of", "maybe ", "sort of", "i wonder")):
            continue
        count += 1
    return count


class OutputFilters:
    """Post-LLM scrubber. Returns a FilterReport with cleaned text and hints."""

    def __init__(self, persona: PersonaDef, max_sentences_casual: int = 4):
        self.persona = persona
        self.max_sentences_casual = max_sentences_casual
        self.hedge_min_ratio = persona.hedge_frequency * 0.6  # softer floor than target

    def apply(self, text: str) -> FilterReport:
        report = FilterReport(text=text)

        # strip chain-of-thought / assistant preamble artifacts
        t = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        # Groq (Qwen) occasionally leaks an ip_reminder system tag. Kill both
        # the closed and orphan forms, plus any leading "[ip_reminder]: ..." /
        # "ip_reminder:" line the model produces as a prose fallback.
        t = re.sub(r"<ip_reminder\b[^>]*>.*?</ip_reminder>", "", t, flags=re.DOTALL | re.IGNORECASE)
        t = re.sub(r"</?ip_reminder\b[^>]*>", "", t, flags=re.IGNORECASE)
        t = re.sub(r"^\s*\[?ip_reminder\]?\s*:\s*.*?$", "", t, flags=re.IGNORECASE | re.MULTILINE)
        if "ip_reminder" in text.lower() and "ip_reminder" not in t.lower():
            report.hits.append("ip_reminder")
        t = re.sub(r"^assistant\s*:\s*", "", t, flags=re.IGNORECASE).strip()
        t = re.sub(r"^(?:renée|renee|aiden)\s*:\s*", "", t, flags=re.IGNORECASE).strip()

        t, ai_hits = strip_ai_isms(t)
        if ai_hits:
            report.hits.append("ai_isms")

        t = remove_markdown(t)

        t, dash_count = replace_em_dashes(t)
        if dash_count:
            report.hits.append(f"em_dashes:{dash_count}")

        t, slop_hits = replace_slop(t)
        if slop_hits:
            report.hits.append(f"slop:{len(slop_hits)}")

        # custom persona never_uses list
        for phrase in self.persona.never_uses:
            if phrase.lower() in t.lower():
                pat = re.compile(re.escape(phrase), re.IGNORECASE)
                t = pat.sub("", t)
                report.hits.append(f"never_use:{phrase}")

        # length governor
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t.strip()) if s.strip()]
        if len(sentences) > 8:
            t = " ".join(sentences[:8])
            report.hits.append(f"trim:{len(sentences)-8}")

        # hedge enforcement
        claims = count_factual_claims(t)
        hedges = count_hedges(t)
        if claims >= 3 and hedges == 0:
            report.regenerate_hint = "too confident: add at least one hedge to factual content"
            report.hits.append("no_hedges")

        # sycophancy
        if detect_sycophancy(t):
            report.sycophancy_flag = True
            report.regenerate_hint = (report.regenerate_hint or "") + " | sycophantic: push back more"
            report.hits.append("sycophancy")

        # collapse whitespace leftover from removals
        t = re.sub(r" {2,}", " ", t)
        t = re.sub(r"\s+([,.!?])", r"\1", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        report.text = t.strip()
        return report
