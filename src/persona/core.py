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

from ..cognition import (
    AffectScorer,
    FringeState,
    FringeStore,
    LoopTracker,
    PressureComputer,
    RegisterDetector,
    Turn as FringeTurn,
)
from ..eval.metrics import MetricsStore, TurnMetric
from ..identity import ReneeIdentityManager, sign_receipt, CompletionReceipt
from .filters import OutputFilters, FilterReport
from .llm_router import LLMResponse, LLMRouter
from .mood import MoodState, MoodStore
from .persona_def import PersonaDef, load_persona
from .prompt_assembler import build_system_prompt
from .style_rules import StyleReference, load_style_reference

try:  # Safety layer is optional — tests construct PersonaCore without it.
    from ..safety import SafetyLayer
except Exception:  # pragma: no cover
    SafetyLayer = None  # type: ignore


@dataclass
class TurnResult:
    text: str
    mood: MoodState
    llm: LLMResponse
    filters: FilterReport
    retrieved_memories: list[dict]
    receipt: CompletionReceipt
    backend_decision: str
    # Health outcome. `cap_tripped` means the turn just pushed today's total
    # over the configured daily cap and the text has been overridden with
    # the farewell. The orchestrator forwards this to the bridge so it can
    # close the session after the farewell is spoken.
    cap_tripped: bool = False
    cap_already_tripped: bool = False
    cap_minutes_used: float = 0.0
    cap_minutes_limit: float = 0.0
    cap_cooldown_until: float | None = None


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


# Vulnerability markers (first-person emotional disclosure). Upstream check so
# the persona core can tell the reality-anchor injector to stand down on turns
# that carry load-bearing emotional weight. Kept as a tuple of lowercase
# substrings; a substring hit anywhere in the user text is enough.
_VULNERABILITY_MARKERS: tuple[str, ...] = (
    "i feel",
    "i'm feeling",
    "i've been feeling",
    "i am feeling",
    "i'm scared",
    "i am scared",
    "i'm terrified",
    "i'm anxious",
    "i'm worried",
    "i'm overwhelmed",
    "i'm lonely",
    "i've been lonely",
    "i'm alone",
    "i feel alone",
    "feeling alone",
    "really alone",
    "i don't know what to do",
    "i don't know how",
    "help me understand",
    "i need you",
    "i miss",
    "i'm hurting",
    "i'm sad",
    "i'm depressed",
    "i've been sad",
    "i'm falling apart",
    "can't do this",
    "don't want to",
    "i'm scared of",
    "scares me",
    "this is hard",
    "i'm struggling",
    "i've been struggling",
    "i'm breaking",
    "broken",
    "i'm not okay",
    "i am not okay",
    "honestly i",
    "honestly, i",
    "can i tell you",
    "vulnerable",
    "ashamed",
    "embarrassed",
)


def _contains_vulnerability_marker(user_text: str) -> bool:
    if not user_text:
        return False
    lower = user_text.lower()
    return any(marker in lower for marker in _VULNERABILITY_MARKERS)


def _build_anchor_ctx_flags(
    *,
    user_text: str,
    user_tone: dict,
    new_mood: MoodState,
    regenerate_hint: str | None,
) -> dict:
    """Build the ctx_flags dict the reality-anchor injector consults.

    The anchor layer suppresses on `high_intensity` or `vulnerable` so that
    an emotionally load-bearing beat never gets interrupted by a meta
    acknowledgement of Renée's nature. `corrective` is surfaced but NOT
    suppressed; a regenerated corrective turn already broke the flow, so an
    anchor in that window is fine.
    """
    intensity = float(user_tone.get("intensity", 0.0) or 0.0)
    flags: dict[str, bool] = {
        "high_intensity": intensity > 0.7,
        "vulnerable": _contains_vulnerability_marker(user_text),
        "corrective": bool(regenerate_hint and "sycophantic" in regenerate_hint.lower()),
    }
    # Expose mood context for downstream consumers that want to read it
    # without re-implementing mood access. Not used by the current anchor
    # suppression rules — included so an observability dashboard or a
    # future suppression rule can read the same dict without threading a
    # separate state object.
    flags["_mood_warmth"] = float(new_mood.warmth)
    flags["_mood_patience"] = float(new_mood.patience)
    return flags


class PersonaCore:
    def __init__(
        self,
        persona_name: str = "renee",
        config_dir: str | Path = "configs",
        state_dir: str | Path = "state",
        router: LLMRouter | None = None,
        memory_store=None,  # duck-typed, imported lazily
        safety_layer=None,  # Optional[SafetyLayer]
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
        self.safety_layer = safety_layer

        # Cognition layer (M16). Per-persona fringe state plus stateless
        # heuristic scorers/trackers. Only active when FRINGE_ENABLED env
        # var is true; otherwise the components exist but update() is never
        # called. See DECISIONS.md for the architectural rationale.
        embedder = self.memory_store.embedding if self.memory_store is not None else None
        embedder_dim = embedder.dim if embedder is not None else 384
        # FRINGE_PERSIST_PATH overrides the directory if set; otherwise the
        # fringe lives alongside mood/identity in state_dir.
        fringe_dir = Path(os.getenv("FRINGE_PERSIST_PATH", str(self.state_dir)))
        self.fringe_store = FringeStore(self.persona_name, fringe_dir)
        self.fringe = self.fringe_store.load(embedding_dim=embedder_dim)
        self._fringe_embedder = embedder
        self._affect_scorer = AffectScorer()
        self._register_detector = RegisterDetector()
        self._loop_tracker = LoopTracker()
        self._pressure_computer = PressureComputer(embedder=embedder)

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
                fringe_bias = None
                bias_weight = 0.0
                if (
                    os.getenv("FRINGE_ENABLED", "false").lower() == "true"
                    and self.fringe.turn_count > 0
                ):
                    fringe_bias = self.fringe.to_retrieval_bias()
                    bias_weight = float(os.getenv("FRINGE_RETRIEVAL_WEIGHT", "0.3"))
                retrieved = self.memory_store.retrieve(
                    user_text,
                    mood=mood,
                    k=8,
                    retrieval_bias=fringe_bias,
                    bias_weight=bias_weight,
                )
            except Exception as e:  # memory should not kill a turn
                retrieved = []

        # 3. prompt assembly
        fringe_prefix: str | None = None
        if (
            os.getenv("FRINGE_ENABLED", "false").lower() == "true"
            and self.fringe.turn_count > 0
        ):
            fringe_prefix = self.fringe.to_prompt_prefix()
        system_prompt = build_system_prompt(
            self.persona,
            mood,
            retrieved_memories=retrieved,
            core_facts=core_facts,
            style_reference=self.style_reference,
            fringe_prefix=fringe_prefix,
        )
        # PII scrub on the user-facing text + message history + core facts
        # before handing anything to a cloud LLM. Mapping is unscrubbed on
        # the response path so the user sees real names again.
        scrub_mapping: dict[str, str] = {}
        llm_user_text = user_text
        llm_messages_source = history + [{"role": "user", "content": user_text}]
        llm_system = system_prompt
        if self.safety_layer is not None:
            u = self.safety_layer.pre_llm(user_text)
            llm_user_text = u.text
            scrub_mapping.update(u.mapping)
            llm_messages_source = []
            for m in history:
                content = m.get("content", "")
                sr = self.safety_layer.pre_llm(content)
                llm_messages_source.append({**m, "content": sr.text})
                scrub_mapping.update(sr.mapping)
            llm_messages_source.append({"role": "user", "content": llm_user_text})
            sys_sr = self.safety_layer.pre_llm(system_prompt)
            llm_system = sys_sr.text
            scrub_mapping.update(sys_sr.mapping)

        messages = llm_messages_source

        # 4. LLM call (with one regen if filters flag)
        chosen_backend = backend or self.router.decide_backend(user_text)
        llm_resp = self.router.generate(
            system_prompt=llm_system,
            messages=messages,
            backend=chosen_backend,
            user_text=llm_user_text,
        )
        if scrub_mapping and self.safety_layer is not None:
            # Unscrub tokens in the LLM output so filters + memory see real refs.
            llm_resp.text = self.safety_layer.unscrub(llm_resp.text, scrub_mapping)
        report = self.filters.apply(llm_resp.text)

        regen_fired = False
        if report.regenerate_hint:
            # one retry with a system-level correction note
            retry_system = llm_system + f"\n\nREGEN NOTE: prior attempt flagged ({report.regenerate_hint}). Fix it. Keep the voice."
            llm_resp2 = self.router.generate(
                system_prompt=retry_system,
                messages=messages,
                backend=chosen_backend,
                user_text=llm_user_text,
                temperature=0.9,
            )
            if scrub_mapping and self.safety_layer is not None:
                llm_resp2.text = self.safety_layer.unscrub(llm_resp2.text, scrub_mapping)
            report2 = self.filters.apply(llm_resp2.text)
            if len(report2.hits) <= len(report.hits):
                llm_resp = llm_resp2
                report = report2
                regen_fired = True

        # 5. mood update based on user tone. Moved ahead of the anchor step
        # so the anchor suppression classifier can read the post-tone mood
        # (warmth / patience) plus the inferred intensity when deciding
        # whether the turn is load-bearing.
        tone = _infer_user_tone(user_text)
        new_mood = self.mood_store.apply_tone(mood, tone)

        # 6. reality-anchor injection (soft, ~1 in 50 turns; suppressed on
        # load-bearing emotional beats). Context flags wired from the same
        # signals the orchestrator uses: high intensity, first-person
        # vulnerability markers, and the output filters' sycophancy regen
        # hint. An anchor on a vulnerable beat is the worst possible
        # immersion break, so that beat always wins.
        if self.safety_layer is not None:
            ctx_flags = _build_anchor_ctx_flags(
                user_text=user_text,
                user_tone=tone,
                new_mood=new_mood,
                regenerate_hint=report.regenerate_hint,
            )
            anchor_res = self.safety_layer.maybe_anchor(report.text, ctx_flags=ctx_flags)
            if anchor_res.injected:
                report.text = anchor_res.text
                report.hits.append(f"anchor:{anchor_res.phrase[:24]}")

        # 7. memory write (post-mood, post-anchor)
        if self.memory_store is not None:
            try:
                self.memory_store.write_turn(user_text=user_text, assistant_text=report.text, mood=new_mood)
            except Exception:
                pass

        # 7a. fringe update (post-memory, pre-telemetry). Toggled by
        # FRINGE_ENABLED. Per-persona slow state biasing the next turn's
        # retrieval and prompt prefix. Failures are swallowed inside
        # FringeState.update so a broken fringe never breaks the turn.
        # Persisted after every update so cross-session continuity survives
        # crashes mid-conversation.
        if os.getenv("FRINGE_ENABLED", "false").lower() == "true" and self._fringe_embedder is not None:
            self.fringe.update(
                turn=FringeTurn(
                    user=user_text,
                    assistant=report.text,
                    mood=new_mood,
                ),
                embedder=self._fringe_embedder,
                affect_scorer=self._affect_scorer,
                register_detector=self._register_detector,
                loop_tracker=self._loop_tracker,
                pressure_computer=self._pressure_computer,
            )
            try:
                self.fringe_store.save(self.fringe)
            except Exception:
                pass  # persistence failure must not break the turn

        # 8. UAHP completion receipt + health cap evaluation. Duration is
        # measured before the receipt is signed so the receipt reflects the
        # full turn.
        duration_ms = (time.time() - t0) * 1000
        cap_outcome = None
        if self.safety_layer is not None:
            cap_outcome = self.safety_layer.record_turn_duration(duration_ms)
            if cap_outcome is not None and cap_outcome.just_tripped:
                # Override the reply with the farewell. The orchestrator
                # will see cap_tripped=True on the TurnResult and close the
                # audio bridge after this utterance is spoken.
                report.text = cap_outcome.farewell
                report.hits.append("cap_tripped")
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
            cap_tripped=bool(cap_outcome and cap_outcome.just_tripped),
            cap_already_tripped=bool(cap_outcome and cap_outcome.already_tripped),
            cap_minutes_used=float(cap_outcome.minutes_used if cap_outcome else 0.0),
            cap_minutes_limit=float(cap_outcome.minutes_cap if cap_outcome else 0.0),
            cap_cooldown_until=(cap_outcome.cooldown_until if cap_outcome else None),
        )
