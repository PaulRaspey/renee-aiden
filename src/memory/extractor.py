"""
Candidate memory extraction.

Uses a small LLM (Ollama Gemma) to pull 0-3 memory-worthy facts from a turn
and classify them. Falls back to a heuristic extractor if Ollama is
unavailable so offline dev still works.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

try:
    import ollama  # type: ignore
except ImportError:  # pragma: no cover
    ollama = None  # type: ignore


EXTRACT_SYSTEM = """You analyze a conversation turn and extract at most 3 memory-worthy facts about the user.
Respond with a JSON array. Each item has:
  content: single short sentence, first-person reference to the user as "PJ"
  tier: one of [ephemeral, casual, significant, inside_joke, core, sensitive]
  emotional_valence: float in [-1, 1], negative sad/angry, positive happy/connected
  emotional_intensity: float in [0, 1]
  salience: float in [0, 1]
  tags: array of short topic tags
  contextual_triggers: array of words/phrases that should surface this later
Rules:
  - If the turn is empty small talk, return []
  - Bias toward fewer, higher-quality memories
  - "core" is reserved for identity facts (family, work, health, ongoing commitments)
  - "significant" for emotional moments, strong opinions, commitments, plans
  - "inside_joke" only for clearly humorous shared references
  - "sensitive" for grief, trauma, health scares, relationship struggles
Respond ONLY with the JSON array, no prose.
"""


@dataclass
class MemoryExtractor:
    ollama_host: str | None = None
    ollama_model: str | None = None

    def __post_init__(self):
        self.client = None
        if ollama is None:
            return
        host = self.ollama_host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.client = ollama.Client(host=host)
        self.model = self.ollama_model or os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

    def extract(self, user_text: str, assistant_text: str) -> list[dict]:
        if self.client is None:
            return self._heuristic(user_text, assistant_text)
        try:
            prompt = (
                f"USER: {user_text}\nASSISTANT: {assistant_text}\n\n"
                "Extract memory candidates as a JSON array now."
            )
            resp = self.client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": 0.2, "num_predict": 400},
            )
            text = resp.get("message", {}).get("content", "") or ""
            return _parse_json_array(text) or self._heuristic(user_text, assistant_text)
        except Exception:
            return self._heuristic(user_text, assistant_text)

    def _heuristic(self, user_text: str, assistant_text: str) -> list[dict]:
        out: list[dict] = []
        ut = user_text.strip()
        if not ut:
            return out
        # Only capture "I" statements from the user, first sentence, short
        sentences = re.split(r"(?<=[.!?])\s+", ut)
        for s in sentences[:2]:
            s_strip = s.strip()
            if len(s_strip) < 8 or len(s_strip) > 240:
                continue
            lower = s_strip.lower()
            if not any(tok in lower for tok in ["i ", "my ", "i'm ", "i've ", "we "]):
                continue
            tier = "casual"
            valence = 0.0
            intensity = 0.3
            if any(w in lower for w in ["love", "happy", "proud", "excited"]):
                valence = 0.6
                intensity = 0.6
                tier = "significant"
            if any(w in lower for w in ["hurt", "sad", "grief", "died", "scared", "worried", "anxious"]):
                valence = -0.6
                intensity = 0.7
                tier = "sensitive"
            if any(w in lower for w in ["work", "kid", "son", "daughter", "wife", "husband", "partner", "teach", "teaching"]):
                tier = "core"
                intensity = 0.5
            content = s_strip.replace(" I ", " PJ ")
            if content.lower().startswith("i "):
                content = "PJ " + content[2:]
            tags = []
            for kw in ["work", "family", "health", "food", "music", "teaching", "project"]:
                if kw in lower:
                    tags.append(kw)
            out.append({
                "content": content,
                "tier": tier,
                "emotional_valence": valence,
                "emotional_intensity": intensity,
                "salience": 0.5,
                "tags": tags,
                "contextual_triggers": tags,
            })
            if len(out) >= 2:
                break
        return out


def _parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    # strip markdown fences, if any
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out: list[dict] = []
    if not isinstance(data, list):
        return []
    for item in data:
        if not isinstance(item, dict) or "content" not in item:
            continue
        out.append({
            "content": str(item["content"]),
            "tier": str(item.get("tier", "casual")).lower(),
            "emotional_valence": float(item.get("emotional_valence", 0.0)),
            "emotional_intensity": float(item.get("emotional_intensity", 0.3)),
            "salience": float(item.get("salience", 0.5)),
            "tags": list(item.get("tags", [])),
            "contextual_triggers": list(item.get("contextual_triggers", item.get("tags", []))),
        })
    return out[:3]
