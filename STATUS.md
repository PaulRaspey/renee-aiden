# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** M0, M2, M3, M4 all green
**Last commit:** `M2/M3/M4 acceptance suite: live Groq, all three pass`
**Next milestone:** M1 (ASR) on the voice side, or M11 (eval harness) while text is hot
**Blockers:** None

## What's Done

- [x] Full architecture specification
- [x] All eight stack deep dives (voice, persona, memory, paralinguistics, turn-taking, eval, UAHP, cloud)
- [x] Persona configs for Renée and Aiden
- [x] Prosody rules starter, humanness probes, bootstrap, safety framework, copyright handling
- [x] Git repo initialized, pushed to github.com/PaulRaspey/renee-aiden (private)
- [x] M0: UAHP identity primitives + agent-per-component keyfiles
  - Ed25519 pattern was the spec; HMAC-SHA256 is what ships upstream in `uahp-stack/core.py`.
  - We vendored the working pattern. Swap to the fixed PyPI `uahp` when it lands.
- [x] M0: src scaffolding and dependency install (Python 3.12 venv)
- [x] M2: persona core text-only
  - persona YAML load
  - six-axis mood state with SQLite persistence
  - system prompt assembler (persona + mood + memory + rotating quirk + rules)
  - output filters (AI-isms, em-dash, slop, markdown, hedges, sycophancy, length)
  - LLM router: Groq Qwen3-32B (reasoning_effort=none), Ollama Gemma, Anthropic fallback
  - BOM-tolerant bridge-key parser, Groq rate-limit retry with backoff
  - PersonaCore.respond() signs a UAHP CompletionReceipt per turn, retries once if filters flag
- [x] M3: mood state + persistence
  - circadian envelope (energy low at 3am, high at noon)
  - baseline drift toward persona defaults
  - tone-driven updates (patience drops on anger, warmth follows user warmth)
- [x] M4: emotionally-weighted memory
  - SQLite + FAISS store, tier-weighted retrieval, recency decay with spike-on-reference
  - sensitive tier hard-filtered unless user raises it
  - core-tier facts seeded on first init (six PJ facts)
  - memory extractor: Ollama Gemma JSON extraction with heuristic fallback (extractor
    currently disabled in the acceptance suite for speed; chat REPL uses it live)
- [x] M2/M3/M4 acceptance suite `tests/acceptance/run_acceptance.py` — all three green
  - M2: sycophancy 0/20, pushback 3/3, opinion pairs 3/3, reality anchor respected
  - M3: circadian ok, patience drops on anger, recovery on idle
  - M4: 3/4 callbacks land naturally (spec min 1, our bar ≥3)
- [x] CLI: `python -m src.cli.chat` with rich rendering, /mood /memories /retrieve /receipt /quit
- [x] Unit tests (20/20 passing): identity, filters, mood, memory, persona configs
- [ ] M0 audio I/O round-trip test (DEFERRED — text-first path per PJ)

## What's Next

- [ ] M1 ASR (once audio is back on the table)
- [ ] M5 TTS (needs reference audio + rented GPU)
- [ ] M6-M10 paralinguistics, prosody, turn-taking, backchannel, integration
- [ ] M11 evaluation harness with dashboard, nightly runs

## Known Risks

- T400 GPUs (4GB each) too weak for realtime stack. PJ plans rented GPU for M5 onward.
- Groq free tier TPM limit (6000 tpm for qwen3-32b) — acceptance suite hits it occasionally.
  Router now backs off and retries on 429. Upgrade to Dev Tier when we do heavy eval nightly.
- Ollama has only `gemma4:e4b` (9.6GB) — too big for the T400s, runs CPU-only and is slow
  (~25s per extraction call). Disabled in the acceptance suite. For live chat, the heuristic
  extractor fallback keeps things usable; pull a small model (`gemma3:1b` or `qwen2.5:1.5b`)
  when you have a spare minute.
- XTTS-v2 model download (~2GB) deferred to M5.
- No reference audio for Renée/Aiden yet. PJ to record between M4 and M5.
- PyPI `uahp==0.5.4` wheel ships broken. Local identity shim in `src/identity/uahp_identity.py`
  stays until upstream is fixed. Same public API either way.

## Notes for PJ

- Start a chat: `python -m src.cli.chat` (Renée) or `python -m src.cli.chat --persona aiden`.
  Batch launcher: `scripts\chat.bat` (Renée default).
- Commands in the REPL: `/mood`, `/memories`, `/retrieve <query>`, `/receipt`, `/quit`.
- Mood persists in `state/<persona>_mood.db`, memories in `state/<persona>_memory.db`.
- Identities persist in `state/identities/*.key.json`. They are signing keys. Do NOT commit —
  `.gitignore` already covers `state/`.
- Run the acceptance suite against live Groq: `python -m tests.acceptance.run_acceptance`.
  It takes 8-10 minutes because of rate-limit throttling; writes the report to
  `tests/acceptance/last_run.md`.
- Upload *Her* script when ready for M12. Script text only, no audio.
- Record reference voice sessions ASAP — see `architecture/01_voice.md` for spec.
- Budget first month rent on RunPod around $400-500 for dev iteration.
