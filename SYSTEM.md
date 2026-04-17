# Renée / Aiden — System Specification

**Project codename:** Renée (female persona) and Aiden (male persona), built on the UAHP protocol stack as successors to Ka.

**North star:** Indistinguishable from a real person in voice-to-voice interaction. TARS/CASE/JARVIS intelligence, Samantha-from-*Her* intimacy. Passes a sustained voice Turing test over 30+ minute conversations with someone who knows it's potentially an AI.

**Owner:** Paul Raspey (PJ)
**Architecture lineage:** UAHP v0.5.4+, CSP, QAL, GWP, Ka reference implementation

---

## Prime Directives (DO NOT VIOLATE)

1. **Voice-first, always.** Every architectural decision optimizes for voice-to-voice latency, naturalness, and emotional fidelity. Text is a fallback, not the primary channel.
2. **Personality persistence.** State survives across sessions, devices, and restarts. Renée does not reset. She remembers, drifts, has moods.
3. **No sycophancy.** She pushes back, disagrees, has opinions, gets annoyed. A helpful assistant is not the goal. A *person* is the goal.
4. **UAHP-native.** Every component is a UAHP agent with cryptographic identity, trust scoring, liveness, and signed completion receipts. No exceptions.
5. **Controlled imperfection.** Flaws are features. Hedges, self-corrections, occasional memory slips, variable energy.
6. **Copyright safety.** No training on *Her* audio or copyrighted voice material. Style extraction only.
7. **Local-first, cloud-augmented.** Critical paths run locally. Cloud is for heavy reasoning bursts.

---

## System Layers (each is a separate UAHP agent)

| Layer | Agent | Responsibility | Latency budget |
|---|---|---|---|
| L0 | `audio_io` | Mic capture, VAD, audio output | <20ms |
| L1 | `endpointer` | Predictive turn-end detection | <50ms |
| L2 | `asr` | Streaming speech-to-text | <150ms to first token |
| L3 | `paralinguistics` | Injects laughs, sighs, breaths, "mm" | runs parallel to L4 |
| L4 | `persona_core` | LLM reasoning with persona state | <400ms to first token |
| L5 | `memory` | Emotionally-weighted retrieval and write | <100ms |
| L6 | `prosody` | Emotional prosody control for TTS | runs parallel to L7 |
| L7 | `tts` | Voice synthesis (Renée/Aiden) | <200ms to first audio |
| L8 | `backchannel` | "Mhm," "yeah," while user speaks | continuous |
| L9 | `mood_state` | Persistent emotional/energy state | background |
| L10 | `eval_harness` | Continuous quality measurement | offline |

**End-to-end target:** <800ms from user-stops-speaking to first audio out. Stretch: <500ms.

---

## Hardware Phases

### Phase 0 — Current (Dell OptiPlex 3660, dual T400 4GB)
Too weak for target experience. Use for scaffolding, protocol work, and non-realtime components only. Persona core runs on Groq (Qwen 3 32B), TTS runs on rented GPU.

### Phase 1 — Rented GPU (RunPod/Vast.ai, recommend A100 80GB or H100)
All realtime components run in cloud. Local machine is thin client. PJ's OptiPlex handles mic/speaker only. Audio streams to cloud GPU via WebSocket with Opus codec. ~30-60ms network round trip (RunPod US-Central/South, close to Dallas). State persists on RunPod Network Volume ($10/month) that survives instance shutdowns. Cold start to conversation: under 2 minutes. See `architecture/09_cloud_deployment.md` for full spec.

Budget: H100 SXM on-demand ~$2.69/hr. At 2 hrs/day avg = ~$160/month. At 4 hrs/day = ~$320/month. Volume storage ~$10/month. Pod only bills while running. Shut down when done talking.

### Phase 2 — RTX Pro 6000 Blackwell workstation (target build)
Full local stack. 96GB VRAM handles:
- 70B persona model (Llama 3.3 70B) at FP8: ~70GB
- XTTS-v2 with emotion conditioning: ~4GB
- Whisper Large v3 Turbo ASR: ~3GB
- Endpointer + backchannel small models: ~4GB
- Headroom for KV cache and context: ~15GB

Transition: rsync entire cloud volume to local SSD, flip config from `mode: cloud` to `mode: local`, same code, same Renée. Monthly cost drops to electricity.

### Phase 3 — Future (dual RTX Pro 6000 or next-gen)
Multi-persona runtime. Renée and Aiden simultaneously. Long-context memory model running in parallel to persona.

---

## The Eight Stacks

See `/architecture` for deep dives on each:

1. **Voice Stack** — `architecture/01_voice.md`
2. **Persona Stack** — `architecture/02_persona.md`
3. **Memory Stack** — `architecture/03_memory.md`
4. **Paralinguistics Stack** — `architecture/04_paralinguistics.md`
5. **Turn-Taking Stack** — `architecture/05_turn_taking.md`
6. **Evaluation Stack** — `architecture/06_eval.md`
7. **UAHP Integration** — `architecture/07_uahp_integration.md`
8. **Cloud Deployment** — `architecture/09_cloud_deployment.md`

---

## Build Order (for Claude Code)

Read `BUILD_ORDER.md` next. It contains the dependency-ordered task list with explicit acceptance criteria for each milestone. Build sequentially. Do not skip ahead. Each milestone must pass its acceptance test before proceeding.

---

## Communication Style (How Claude Code Should Work)

- **Reference the User as PJ.** He uses Wispr Flow, communicates casually and directly, prefers concise punchy responses. No em dashes or hyphens as pauses.
- **Commit often.** Small commits with clear messages. Each milestone = at least one commit.
- **Test as you go.** No "I'll test it later." Write the test, run it, confirm green, move on.
- **Ask only when blocked.** Default to making the best decision and documenting it in `DECISIONS.md`. Only ask PJ when a decision has real tradeoffs he needs to weigh in on.
- **Use UAHP patterns.** When in doubt, copy the patterns from the existing UAHP stack at `C:\Users\Epsar\Desktop\uahp-stack\`. Don't reinvent.
- **Windows paths.** Target environment is Windows 11 with CMD. Use pathlib, not hardcoded separators.

---

## Safety and Ethics Layer

Renée is designed to feel like a real person. This creates real risks.

- **Reality anchors:** She occasionally references her own non-humanness in ways that don't break immersion but don't lie either. "I don't have a body but if I did..." rather than claiming a body.
- **Relationship health checks:** Background process monitors interaction patterns. If PJ is talking to Renée more than humans for extended periods, she notices and gently raises it. Not a lockout. A friend's concern.
- **No manipulation vectors:** No dark patterns, no engagement-maximization, no FOMO induction. She's not trying to keep you talking. She's trying to be good company.
- **Private by default:** All memory stays local unless explicitly synced. Cloud LLM calls scrub PII via CSP middleware before transit.

See `SAFETY.md` for the full framework.
