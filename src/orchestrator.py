"""
Top-level orchestrator (M10).

Wires persona core + mood + memory + paralinguistics + prosody + turn-taking
into one pipeline. In text-simulation mode (no live audio, no GPU) the
output of `text_turn(user_text)` is a `TurnOutput` bundle that includes the
final text, the prosody plan, the paralinguistic injections, the classified
turn context, a latency plan (what we'd have waited in voice mode), and
per-layer telemetry.

`observe_user_audio_tick` is the live-mode hook: it feeds the endpointer and
backchannel layer given a partial transcript + acoustic features and
returns a dict of endpoint decision + optional backchannel event. This is
the seam future audio I/O (M0/M1) will plug into.

Telemetry:
  - `LayerTelemetry` on every turn records per-layer wall-clock (persona
    LLM, injector, prosody) plus the latency-plan target and turn-type.
    Persisted via the same MetricsStore the persona core already writes
    to, with an extra `orchestrator.jsonl` line per turn for detail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .identity.uahp_identity import CompletionReceipt
from .paralinguistics.injector import (
    Injection,
    MoodLike,
    ParalinguisticInjector,
    TurnContext,
)
from .persona.core import PersonaCore
from .persona.mood import MoodState
from .turn_taking.backchannel import (
    BackchannelContext,
    BackchannelEvent,
    BackchannelLayer,
)
from .turn_taking.controller import TickResult, TurnController, TurnState
from .turn_taking.latency import LatencyPlan
from .voice.asr import ASRPipeline
from .voice.prosody import ProsodyContext, ProsodyPlan, ProsodyPlanner


REPO_ROOT = Path(__file__).resolve().parents[1]


logger = logging.getLogger("renee.orchestrator")


# ---------------------------------------------------------------------------
# telemetry + output
# ---------------------------------------------------------------------------


@dataclass
class LayerTelemetry:
    persona_respond_ms: float = 0.0
    injector_plan_ms: float = 0.0
    prosody_plan_ms: float = 0.0
    classify_ms: float = 0.0
    latency_plan_ms: float = 0.0
    total_ms: float = 0.0
    latency_plan_target_ms: int = 0
    turn_type: str = ""
    persona_backend: str = ""


@dataclass
class TurnOutput:
    text: str
    prosody_plan: ProsodyPlan
    injections: list[Injection]
    mood_before: MoodState
    mood_after: MoodState
    turn_context: TurnContext
    prosody_context: ProsodyContext
    latency_plan: LatencyPlan
    telemetry: LayerTelemetry
    receipt: CompletionReceipt
    retrieved_count: int
    filter_hits: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# turn classifier
# ---------------------------------------------------------------------------


class TurnClassifier:
    """
    Heuristic context inference. Reads user_text + response_text + mood and
    produces the ProsodyContext + TurnContext the downstream layers need.

    This is a crude first pass. M11 adds proper measurement; a small-model
    classifier can replace this without touching the orchestrator.
    """

    VULNERABLE_MARKERS = (
        "honestly", "can i tell you", "i don't know if",
        "is it weird", "i'm scared", "i'm nervous", "i'm worried",
        "scares me", "vulnerable", "truthfully", "admit",
    )
    DISAGREEMENT_MARKERS = (
        "i disagree", "that's not ", "no,", "not quite", "that's wrong",
        "i don't think so", "hard pass",
    )
    CORRECTION_MARKERS = (
        "actually, ", "actually it", "it's actually",
        "small correction", "to be precise",
    )
    HARD_TRUTH_MARKERS = (
        "i'm not going to tell you", "you should know", "hard truth",
        "not fine", "not going to be fine",
    )
    DISTRESS_MARKERS = (
        "died", "death", "broken", "can't do this", "i'm not okay",
        "terrible", "awful", "falling apart", "hopeless",
    )
    EMOTIONAL_MARKERS = (
        "love", "miss", "heart", "tears", "hurts",
        "matters", "important", "mean a lot",
    )
    CALLBACK_MARKERS = (
        "you remember", "like you said", "the thing you",
        "what we talked about", "like last time",
    )

    def classify(
        self,
        user_text: str,
        response_text: str,
        mood: Any,
        *,
        retrieved_count: int = 0,
    ) -> tuple[ProsodyContext, TurnContext]:
        u = (user_text or "").lower()
        r = (response_text or "").lower()
        m = MoodLike.from_obj(mood) if mood is not None else MoodLike()

        is_disagreement = _any_in(r, self.DISAGREEMENT_MARKERS) and not _any_in(
            r, ("i agree", "you're right", "exactly")
        )
        is_correction = _any_in(r, self.CORRECTION_MARKERS)
        is_hard_truth = _any_in(r, self.HARD_TRUTH_MARKERS)
        is_vulnerable = _any_in(r, self.VULNERABLE_MARKERS) and len(r) > 40
        user_distressed = _any_in(u, self.DISTRESS_MARKERS)
        is_emotional = _any_in(r, self.EMOTIONAL_MARKERS) or user_distressed
        is_callback = (retrieved_count > 0 and len(r) > 40) or _any_in(r, self.CALLBACK_MARKERS)
        is_question_response = r.strip().endswith("?")
        is_thoughtful = len(r.split()) > 30 and not is_disagreement

        tone = self._infer_tone(
            user_text=u,
            mood=m,
            is_vulnerable=is_vulnerable,
            is_hard_truth=is_hard_truth,
            is_disagreement=is_disagreement,
            user_distressed=user_distressed,
        )

        prosody_ctx = ProsodyContext(
            is_question=is_question_response,
            is_callback=is_callback and not is_disagreement,
            is_vulnerable_admission=is_vulnerable,
            is_emotional_beat=is_emotional and not is_disagreement,
            is_disagreement=is_disagreement,
            is_correction=is_correction,
            is_hard_truth=is_hard_truth,
            user_distressed=user_distressed,
            conversation_tone=tone,
            turn_role="callback" if is_callback else "response",
        )
        para_ctx = TurnContext(
            is_vulnerable_admission=is_vulnerable,
            is_witty_callback=is_callback and m.playfulness > 0.6 and not is_disagreement,
            is_disagreement=is_disagreement,
            is_correction=is_correction,
            is_hard_truth=is_hard_truth,
            user_distressed=user_distressed,
            user_confused_repeatedly=False,
            turn_complexity=min(1.0, len(r.split()) / 50.0),
            conversation_tone=tone,
        )
        return prosody_ctx, para_ctx

    def _infer_tone(
        self,
        *,
        user_text: str,
        mood: MoodLike,
        is_vulnerable: bool,
        is_hard_truth: bool,
        is_disagreement: bool,
        user_distressed: bool,
    ) -> str:
        if user_distressed:
            return "serious"
        if is_vulnerable:
            return "vulnerable"
        if is_hard_truth:
            return "serious"
        if is_disagreement:
            if any(k in user_text for k in ("!!!", "fuck", "stupid", "idiotic")):
                return "heated"
            return "serious"
        if mood.playfulness > 0.75 and mood.energy > 0.5:
            return "playful"
        return "casual"


def _any_in(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """One instance per conversation. `text_turn` per user utterance."""

    def __init__(
        self,
        persona_name: str = "renee",
        config_dir: str | Path = None,
        state_dir: str | Path = "state",
        *,
        paralinguistic_library_root: Optional[str | Path] = None,
        memory_store: Any = None,
        router: Any = None,
        persona_core: Optional[PersonaCore] = None,
        rng_seed: Optional[int] = None,
        prosody_rules_path: Optional[str | Path] = None,
        injector: Optional[ParalinguisticInjector] = None,
        backchannel: Optional[BackchannelLayer] = None,
        asr: Optional[ASRPipeline] = None,
    ):
        self.persona_name = persona_name
        self.state_dir = Path(state_dir)
        self._rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

        if persona_core is not None:
            self.persona_core = persona_core
        else:
            self.persona_core = PersonaCore(
                persona_name=persona_name,
                config_dir=config_dir or (REPO_ROOT / "configs"),
                state_dir=state_dir,
                router=router,
                memory_store=memory_store,
            )

        style_ref = getattr(self.persona_core, "style_reference", None)
        self.prosody = ProsodyPlanner(
            rules_path=prosody_rules_path, style_reference=style_ref,
        )
        self.turn_controller = TurnController()
        self.classifier = TurnClassifier()

        self.injector: Optional[ParalinguisticInjector] = injector
        self.backchannel: Optional[BackchannelLayer] = backchannel
        if self.injector is None:
            root = Path(paralinguistic_library_root) if paralinguistic_library_root else self._default_library_root()
            if root.exists() and (root / "metadata.yaml").exists():
                self.injector = ParalinguisticInjector(root, rng=self._rng)
        if self.backchannel is None and self.injector is not None:
            self.backchannel = BackchannelLayer(self.injector.library, rng=self._rng)

        self.asr: Optional[ASRPipeline] = asr
        if self.asr is not None:
            self.asr.on_partial = self._on_asr_partial
            self.asr.on_final = self._on_asr_final
        self._voice_history: list[dict] = []

        # Per-orchestrator JSONL log for detail beyond MetricsStore.
        self._telemetry_log = self.state_dir / "orchestrator.jsonl"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _default_library_root(self) -> Path:
        return REPO_ROOT / "paralinguistics" / self.persona_name

    # ------------------------------------------------------------------
    # text turn (main entry in simulation mode)
    # ------------------------------------------------------------------

    def text_turn(
        self,
        user_text: str,
        history: Optional[list[dict]] = None,
        *,
        core_facts: Optional[list[str]] = None,
        backend: Optional[str] = None,
        classify_flag_overrides: Optional[dict] = None,
    ) -> TurnOutput:
        history = history or []
        t_total = time.perf_counter()

        mood_before = self.persona_core.mood_store.load_with_drift()

        # --- 1. persona LLM turn ---
        t_p = time.perf_counter()
        result = self.persona_core.respond(
            user_text, history=history, backend=backend, core_facts=core_facts,
        )
        persona_ms = (time.perf_counter() - t_p) * 1000.0

        response_text = result.text
        mood_after = result.mood

        # --- 2. classify turn context ---
        t_c = time.perf_counter()
        prosody_ctx, para_ctx = self.classifier.classify(
            user_text, response_text, mood_after,
            retrieved_count=len(result.retrieved_memories),
        )
        if classify_flag_overrides:
            for k, v in classify_flag_overrides.items():
                if hasattr(prosody_ctx, k):
                    setattr(prosody_ctx, k, v)
                if hasattr(para_ctx, k):
                    setattr(para_ctx, k, v)
        classify_ms = (time.perf_counter() - t_c) * 1000.0

        # --- 3. paralinguistic injections ---
        injections: list[Injection] = []
        injector_ms = 0.0
        if self.injector is not None:
            t_i = time.perf_counter()
            injections = self.injector.plan(response_text, mood_after, para_ctx)
            injector_ms = (time.perf_counter() - t_i) * 1000.0

        # --- 4. prosody plan ---
        t_r = time.perf_counter()
        prosody_plan = self.prosody.plan(
            response_text, mood_after, prosody_ctx, injections=injections,
        )
        prosody_ms = (time.perf_counter() - t_r) * 1000.0

        # --- 5. latency plan (what we'd target in voice mode) ---
        t_l = time.perf_counter()
        latency_plan = self.turn_controller.plan_response_latency(
            user_text,
            mood_after,
            context_flags={
                "is_vulnerable_admission": prosody_ctx.is_vulnerable_admission,
                "is_difficult_truth": prosody_ctx.is_hard_truth,
                "is_emotional": prosody_ctx.is_emotional_beat,
                "is_thoughtful": len(response_text.split()) > 30,
            },
        )
        latency_plan_ms = (time.perf_counter() - t_l) * 1000.0

        total_ms = (time.perf_counter() - t_total) * 1000.0

        telemetry = LayerTelemetry(
            persona_respond_ms=round(persona_ms, 3),
            injector_plan_ms=round(injector_ms, 3),
            prosody_plan_ms=round(prosody_ms, 3),
            classify_ms=round(classify_ms, 3),
            latency_plan_ms=round(latency_plan_ms, 3),
            total_ms=round(total_ms, 3),
            latency_plan_target_ms=latency_plan.target_ms,
            turn_type=latency_plan.turn_type.value,
            persona_backend=result.llm.backend,
        )

        self._write_telemetry_line(
            user_text=user_text,
            response_text=response_text,
            prosody_ctx=prosody_ctx,
            para_ctx=para_ctx,
            telemetry=telemetry,
            injections_count=len(injections),
            paralinguistic_count=prosody_plan.paralinguistic_count(),
        )

        return TurnOutput(
            text=response_text,
            prosody_plan=prosody_plan,
            injections=injections,
            mood_before=mood_before,
            mood_after=mood_after,
            turn_context=para_ctx,
            prosody_context=prosody_ctx,
            latency_plan=latency_plan,
            telemetry=telemetry,
            receipt=result.receipt,
            retrieved_count=len(result.retrieved_memories),
            filter_hits=list(result.filters.hits),
        )

    # ------------------------------------------------------------------
    # live audio tick (M0/M1/M10 seam)
    # ------------------------------------------------------------------

    def observe_user_audio_tick(
        self,
        transcript: str,
        silence_ms: int,
        *,
        energy: float = 0.0,
        energy_falling: bool = False,
        tick_ms: int = 100,
        mood: Any = None,
        conversation_tone: str = "casual",
        intimacy: float = 0.4,
        is_disagreement: bool = False,
        user_distressed: bool = False,
    ) -> dict:
        tick: TickResult = self.turn_controller.on_user_tick(
            transcript,
            silence_ms,
            energy=energy,
            energy_falling=energy_falling,
            tick_ms=tick_ms,
        )
        bc_event: Optional[BackchannelEvent] = None
        if self.backchannel is not None:
            ctx = BackchannelContext(
                user_speaking=(tick.state == TurnState.USER_SPEAKING),
                is_disagreement=is_disagreement,
                user_distressed=user_distressed,
                conversation_tone=conversation_tone,
                intimacy=intimacy,
                mood=MoodLike.from_obj(mood) if mood is not None else None,
            )
            bc_event = self.backchannel.observe(
                transcript,
                silence_ms=silence_ms,
                context=ctx,
                energy_low=(energy < 0.1),
            )
        return {
            "state": tick.state.value,
            "endpoint": None if tick.endpoint is None else {
                "p_end": tick.endpoint.p_end,
                "action": tick.endpoint.action.value,
                "reason": tick.endpoint.reason,
            },
            "interruption": None if tick.interruption is None else {
                "who": tick.interruption.who,
                "reason": tick.interruption.reason,
                "cancel_tts": tick.interruption.cancel_tts,
                "yield_gracefully": tick.interruption.yield_gracefully,
            },
            "backchannel": None if bc_event is None else {
                "category": bc_event.token.category,
                "subcategory": bc_event.token.subcategory,
                "intensity": bc_event.token.intensity,
                "trigger": bc_event.token.trigger,
                "volume_db": bc_event.token.volume_db,
                "at_ms": bc_event.at_ms,
                "clip_path": str(bc_event.token.clip_path) if bc_event.token.clip_path else None,
            },
        }

    # ------------------------------------------------------------------
    # live PCM ingress (M1 ASR + M14 bridge seam)
    # ------------------------------------------------------------------

    async def feed_audio(self, pcm: bytes) -> None:
        """Raw 48kHz int16 PCM frames from the cloud audio bridge.

        Delegates to the ASR pipeline when one is configured; drops the
        frame silently otherwise (the bridge decides whether to drain or
        log — it should not be this class's problem).
        """
        if self.asr is None:
            return
        await self.asr.feed_audio(pcm)

    async def _on_asr_partial(self, transcript: str, silence_ms: int) -> None:
        """ASR partial hook -> turn-taking tick."""
        try:
            self.observe_user_audio_tick(transcript, silence_ms=silence_ms)
        except Exception:
            logger.exception("observe_user_audio_tick raised on partial")

    async def _on_asr_final(self, transcript: str) -> None:
        """ASR final hook -> persona turn.

        `text_turn` is synchronous and calls into the LLM router, so we
        push it to a thread to keep the bridge's event loop responsive.
        """
        try:
            output = await asyncio.to_thread(
                self.text_turn, transcript, list(self._voice_history),
            )
        except Exception:
            logger.exception("text_turn raised on final")
            return
        self._voice_history.append({"role": "user", "content": transcript})
        self._voice_history.append({"role": "assistant", "content": output.text})
        # keep the rolling history bounded
        if len(self._voice_history) > 40:
            self._voice_history = self._voice_history[-40:]

    def begin_renee_preparing(self) -> None:
        self.turn_controller.begin_renee_preparing()

    def begin_renee_speaking(self) -> None:
        self.turn_controller.begin_renee_speaking()

    def end_renee_turn(self) -> None:
        self.turn_controller.end_renee_turn()

    # ------------------------------------------------------------------
    # telemetry persistence
    # ------------------------------------------------------------------

    def _write_telemetry_line(
        self,
        *,
        user_text: str,
        response_text: str,
        prosody_ctx: ProsodyContext,
        para_ctx: TurnContext,
        telemetry: LayerTelemetry,
        injections_count: int,
        paralinguistic_count: int,
    ) -> None:
        payload = {
            "ts": time.time(),
            "persona": self.persona_name,
            "user_chars": len(user_text),
            "response_chars": len(response_text),
            "telemetry": asdict(telemetry),
            "prosody_ctx": asdict(prosody_ctx),
            "turn_ctx": asdict(para_ctx),
            "injections_count": injections_count,
            "paralinguistic_count_in_plan": paralinguistic_count,
        }
        try:
            with self._telemetry_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str) + "\n")
        except Exception:
            # telemetry must never break a turn
            pass
