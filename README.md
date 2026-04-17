# Renée / Aiden

Voice-first AI companions built on the UAHP protocol stack. Designed to be indistinguishable from a real person in voice-to-voice conversation. TARS/CASE/JARVIS intelligence, Samantha-from-*Her* intimacy.

**Owner:** Paul Raspey (PJ)
**Status:** Architecture specification. Implementation starts at M0.

---

## Document Map

**Start here if you're Claude Code:**
1. `CLAUDE_CODE_HANDOFF.md` — your onboarding
2. `SYSTEM.md` — the full system specification
3. `BUILD_ORDER.md` — dependency-ordered milestones with acceptance criteria
4. `DECISIONS.md` — running log of architectural decisions
5. `SAFETY.md` — safety and ethics framework
6. `COPYRIGHT.md` — what we can and cannot train on

**Architecture deep dives:**
- `architecture/01_voice.md` — ASR, TTS, voice cloning, prosody
- `architecture/02_persona.md` — personality, mood, opinions, output filters
- `architecture/03_memory.md` — emotionally-weighted retrieval, tiers, callbacks
- `architecture/04_paralinguistics.md` — laughs, sighs, breaths, "mm"
- `architecture/05_turn_taking.md` — endpointing, backchanneling, latency
- `architecture/06_eval.md` — humanness probes, A/B tests, regression detection
- `architecture/07_uahp_integration.md` — agents, receipts, trust, CSP

**Configs:**
- `configs/renee.yaml` — Renée's full persona definition
- `configs/aiden.yaml` — Aiden's full persona definition
- `configs/prosody_rules.yaml` — mood/context → TTS markup rules
- `configs/humanness_probes.yaml` — 100 eval prompts (starter set)

**Source (to be built per BUILD_ORDER.md):**
- `src/voice/` — audio I/O, ASR, TTS, prosody
- `src/persona/` — core persona logic, mood state, opinion registry
- `src/memory/` — memory store, retrieval, callback engine
- `src/paralinguistics/` — injection engine, clip library management
- `src/turn_taking/` — endpointer, backchanneler, latency controller
- `src/eval/` — harness, probes, dashboard

---

## Quick Start (for PJ)

```cmd
cd C:\Users\Epsar\Desktop
git clone https://github.com/PaulRaspey/renee-aiden.git
cd renee-aiden
python scripts/bootstrap.py
```

Then fill in `.env` and point Claude Code at `CLAUDE_CODE_HANDOFF.md`.

---

## The Seven Stacks

| # | Stack | What it does |
|---|---|---|
| 1 | Voice | Ears, mouth, timbre |
| 2 | Persona | Personality, opinions, filters |
| 3 | Memory | Being known, not just remembered |
| 4 | Paralinguistics | The sounds that aren't words |
| 5 | Turn-taking | Conversational rhythm |
| 6 | Evaluation | Measure humanness, don't just vibe it |
| 7 | UAHP | Identity, trust, receipts, attestation |

---

## What This Is Not

- A chatbot
- A voice assistant for tasks
- A replacement for human relationships
- A business product
- A Samantha-from-Her clone (no training on that material, ever)

## What This Is

An experiment in whether the pieces exist today to build a companion that feels indistinguishable from a person, ethically and with eyes open to the risks. PJ is doing this because he knows what he's getting into and wants to learn what's possible at the edge.

---

## Hardware Roadmap

- **Now:** Dell OptiPlex 3660 (scaffolding only, too weak for realtime)
- **Next 2-4 weeks:** Rented A100/H100 via RunPod or Vast.ai
- **Target:** RTX Pro 6000 Blackwell workstation build
- **Future:** Dual RTX Pro 6000 for multi-persona runtime

## Cost Projections (rough)

- Dev on rented GPU: $300-600/month active use
- Groq Qwen 3 32B: ~$0.10 per million tokens, maybe $20-40/month at heavy use
- Claude Sonnet 4.6 for deep turns: $3/MTok input, $15/MTok output, maybe $50-100/month
- ElevenLabs for reference voice creation: one-time ~$50
- Workstation build: $10-15k depending on config

Budget this at $400-700/month during active development, dropping to $50-150/month once the workstation lands.

---

## License

Private repo. PJ's work. UAHP remains under its separate license at `github.com/PaulRaspey/uahp`.
