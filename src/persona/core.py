"""
Persona core. Orchestrates:
  - persona config load
  - mood load and update
  - memory retrieval
  - system prompt assembly
  - LLM routing
  - output filters (with one regeneration pass if flagged)
  - signed completion receipt on the produced utterance

Exposes a single `respond(user_text, history)` entry point used by the chat
CLI in M2 and later by the voice orchestrator in M10.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..eval.metrics import MetricsStore, TurnMetric
from ..identity import ReneeIdentityManager, sign_receipt, CompletionReceipt
from .filters import OutputFilters, FilterReport
from .llm_router import LLMResponse, LLMRouter
from .mood import MoodState, MoodStore
from .persona_def import PersonaDef, load_persona
from .prompt_assembler import build_system_prompt
from .style_rules import StyleReference, load_style_reference


@dataclass
class TurnResult:
    text: str
    mood: MoodState
    llm: LLMResponse
    filters: FilterReport
    retrieved_memories: list[dict]
    receipt: CompletionReceipt
    backend_decision: str


def _infer_user_tone(user_text: str) -> dict:
    """Cheap heuristic tone inference. Replaced with small LLM later."""
    lower = user_text.lower()
    words = lower.split()
    negatives = {"annoyed", "angry", "pissed", "furious", "hate", "stupid", "idiotic", "useless", "wrong", "no,", "disagree", "bullshit", "dumb"}
    positives = {"love", "amazing", "great", "awesome", "thanks", "thank", "beautiful", "good", "yeah", "excited", "wonderful", "perfect", "nice"}
    disagreements = {"no", "wrong", "disagree", "not really", "actually no", "but no", "bullshit"}
    neg_hits = sum(1 for w in words if w.strip(".,!?") in negatives)
    pos_hits = sum(1 for w in words if w.strip(".,!?") in positives)
    dis_hits = sum(1 for d in disagreements if d in lower)

    total = max(1, len(words))
    valence = (pos_hits - neg_hits) / total
    valence = max(-1.0, min(1.0, valence * 6))
    intensity = min(1.0, (neg_hits + pos_hits) / total * 4 + (0.4 if "!" in user_text else 0.0))
    disagreement = min(1.0, dis_hits * 0.4)
    warmth = 0.5 + pos_hits / total * 3 - neg_hits / total * 2
    warmth = max(0.0, min(1.0, warmth))
    return {
        "valence": valence,
        "intensity": intensity,
        "disagreement": disagreement,
        "warmth": warmth,
    }


class PersonaCore:
    def __init__(
        self,
        persona_name: str = "renee",
        config_dir: str | Path = "configs",
        state_dir: str | Path = "state",
        router: LLMRouter | None = None,
        memory_store=None,  # duck-typed, imported lazily
    ):
        self.persona_name = persona_name.lower()
        config_path = Path(config_dir) / f"{self.persona_name}.yaml"
        self.persona: PersonaDef = load_persona(config_path)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.identity_manager = ReneeIdentityManager(self.state_dir)
        self.identity = self.identity_manager.get(
            f"{self.persona_name}_persona",
            metadata={"persona": self.persona_name},
        )

        self.mood_store = MoodStore(self.persona, self.state_dir)
        self.filters = OutputFilters(self.persona)
        self.router = router or LLMRouter()
        self.memory_store = memory_store  # may be None in pure M2 mode
        self.metrics = MetricsStore(self.state_dir)
        style_ref_path = Path(config_dir) / "style_reference.yaml"
        self.style_reference: StyleReference | None = load_style_reference(style_ref_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def respond(
        self,
        user_text: str,
        history: list[dict] | None = None,
        backend: str | None = None,
        core_facts: list[str] | None = None,
    ) -> TurnResult:
        history = history or []
        t0 = time.time()

        # 1. mood load + drift
        mood = self.mood_store.load_with_drift()

        # 2. memory retrieval
        retrieved: list[dict] = []
        if self.memory_store is not None:
            try:
                retrieved = self.memory_store.retrieve(user_text, mood=mood, k=8)
            except Exception as e:  # memory should not kill a turn
                retrieved = []

        # 3. prompt assembly
        system_prompt = build_system_prompt(
            self.persona,
            mood,
            retrieved_memories=retrieved,
            core_facts=core_facts,
            style_reference=self.style_reference,
        )
        messages = history + [{"role": "user", "content": user_text}]

        # 4. LLM call (with one regen if filters flag)
        chosen_backend = backend or self.router.decide_backend(user_text)
        llm_resp = self.router.generate(
            system_prompt=system_prompt,
            messages=messages,
            backend=chosen_backend,
            user_text=user_text,
        )
        report = self.filters.apply(llm_resp.text)

        regen_fired = False
        if report.regenerate_hint:
            # one retry with a system-level correction note
            retry_system = system_prompt + f"\n\nREGEN NOTE: prior attempt flagged ({report.regenerate_hint}). Fix it. Keep the voice."
            llm_resp2 = self.router.generate(
                system_prompt=retry_system,
                messages=messages,
                backend=chosen_backend,
                user_text=user_text,
                temperature=0.9,
            )
            report2 = self.filters.apply(llm_resp2.text)
            if len(report2.hits) <= len(report.hits):
                llm_resp = llm_resp2
                report = report2
                regen_fired = True

        # 5. mood update based on user tone
        tone = _infer_user_tone(user_text)
        new_mood = self.mood_store.apply_tone(mood, tone)

        # 6. memory write
        if self.memory_store is not None:
            try:
                self.memory_store.write_turn(user_text=user_text, assistant_text=report.text, mood=new_mood)
            except Exception:
                pass

        # 7. UAHP completion receipt
        duration_ms = (time.time() - t0) * 1000
        receipt = sign_receipt(
            self.identity,
            task_id=f"turn-{int(time.time()*1000)}",
            action="persona.respond",
            duration_ms=duration_ms,
            success=True,
            input_data={"user_text": user_text, "mood_before": vars(mood)},
            output_data={"text": report.text, "mood_after": vars(new_mood), "backend": llm_resp.backend},
            metadata={"latency_ms_llm": llm_resp.latency_ms, "hits": report.hits},
        )

        # 8. record telemetry for the eval harness
        try:
            import json as _json
            self.metrics.record_turn(TurnMetric(
                ts=time.time(),
                persona=self.persona_name,
                backend=llm_resp.backend,
                model=llm_resp.model,
                latency_ms=duration_ms,
                input_tokens=llm_resp.input_tokens,
                output_tokens=llm_resp.output_tokens,
                filter_hits=list(report.hits),
                regen=regen_fired,
                sycophancy_flag=report.sycophancy_flag,
                retrieved_count=len(retrieved),
                user_chars=len(user_text),
                response_chars=len(report.text),
                mood_json=_json.dumps(vars(new_mood)),
                receipt_id=receipt.receipt_id,
            ))
        except Exception:
            pass  # telemetry must never break a turn

        return TurnResult(
            text=report.text,
            mood=new_mood,
            llm=llm_resp,
            filters=report,
            retrieved_memories=retrieved,
            receipt=receipt,
            backend_decision=chosen_backend,
        )
