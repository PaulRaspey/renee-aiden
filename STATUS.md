# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** M0, M2, M3, M4 all green and pushed. Plus M11-lite (probes, telemetry) and polish.
**Branch:** main
**Repo:** https://github.com/PaulRaspey/renee-aiden (private)
**Last commit:** `M11 lite: humanness probe runner`
**Next milestone:** M1 ASR once voice is back in scope, or M5 when reference audio exists
**Blockers:** None

## How to resume

1. `cd C:\Users\Epsar\Desktop\renee-aiden`
2. `.venv\Scripts\activate`
3. Read `docs/USAGE.md` (commands, state layout, troubleshooting)
4. `python -m src.cli.chat` to talk to Renée; `scripts\chat.bat aiden` for Aiden
5. `python -m tests.acceptance.run_acceptance` to verify M2/M3/M4 still green (8-10 min)

## What's done

- [x] Architecture spec + 8 stack deep dives (pre-session)
- [x] Git repo, pushed to GitHub, private
- [x] `src/identity/` — UAHP-native agent keys, signed receipts (HMAC-SHA256)
- [x] `src/persona/` — persona def, mood (six axes + circadian + drift + bad-day
      floor), prompt assembler, output filters (AI-ism, em-dash, slop,
      hedges, sycophancy, length, markdown strip), dual-backend LLM router
      (Groq Qwen3-32B + Ollama + Anthropic fallback, rate-limit retry), core
- [x] `src/memory/` — SQLite + FAISS, tier-weighted retrieval, sensitive
      hard-filter, core-tier seeded, memory extractor (Ollama JSON w/ heuristic fallback)
- [x] `src/eval/` — turn telemetry store, report CLI, humanness probe runner
- [x] `src/cli/chat.py` — REPL with `/mood /memories /retrieve /receipt /stats
      /save /load /baddayreset /quit`
- [x] `tests/` — 25 unit tests passing; live-Groq acceptance suite passing (M2/M3/M4)
- [x] `docs/USAGE.md` — how to run it all

## What's next (rough order)

- [ ] M1 ASR — needs faster-whisper; install audio deps when voice comes back
- [ ] M5 TTS — needs reference audio captures for Renée and Aiden first
- [ ] M6 paralinguistic library — needs recorded clips
- [ ] M7 prosody control — needs M5 done
- [ ] M8 turn-taking + endpointer — can prototype in text (barge-in timing)
- [ ] M9 backchannel — needs voice
- [ ] M10 end-to-end voice integration
- [ ] M11 full eval harness with dashboard (have probes + telemetry today; missing
      the HTML/trend view and a judge model)
- [ ] M12 *Her* script analysis — PJ uploads the script text
- [ ] M13 safety layer — implicit reality anchor works; PII scrubber and
      relationship-health monitor still to build
- [ ] M14 cloud deployment to RunPod
- [ ] M15 long-running test — 7 days of daily use

## Known risks / gotchas

- **Broken PyPI `uahp==0.5.4` wheel.** It imports submodules that aren't in the
  wheel. We vendor the HMAC pattern from `uahp-stack/core.py` into
  `src/identity/uahp_identity.py`. Swap to the upstream when a fixed version ships.
- **Groq free-tier TPM (6000/min for qwen3-32b).** The router retries with
  backoff parsed from the 429 body; acceptance still takes 8-10 minutes
  because of this. Upgrade to Dev Tier for heavy eval.
- **Ollama has only `gemma4:e4b` (9.6 GB)** on this machine. T400s can't hold
  it, so it runs CPU-only (~25s per extraction call). Memory extractor is
  disabled in the acceptance suite for speed; live chat uses the heuristic
  fallback if the Ollama call errors. Pull a smaller model
  (`ollama pull qwen2.5:1.5b`) whenever convenient.
- **Windows SQLite cleanup** — `tempfile.TemporaryDirectory` sometimes throws
  `PermissionError` on teardown because SQLite holds a handle. Harmless for
  tests; the acceptance runner uses `mkdtemp` + `ignore_errors=True`.
- **`.bridge_key` UTF-8 BOM** — Notepad adds one by default. Router strips it.
- **Audio packages NOT installed** this session: `sounddevice`, `soundfile`,
  `scipy`, `webrtcvad`, `opuslib`, `librosa`, `pyloudnorm`, `faster-whisper`,
  `TTS`. Pull them when M1/M5 start. `requirements.txt` lists them commented.

## Code at a glance

```
src/
├── __init__.py
├── cli/chat.py                   REPL with rich rendering
├── eval/
│   ├── metrics.py                per-turn telemetry store
│   ├── probes.py                 humanness probe runner
│   └── report.py                 CLI report
├── identity/uahp_identity.py     Ed25519-spec, HMAC-real UAHP identity
├── memory/
│   ├── store.py                  SQLite + FAISS + tier weights
│   └── extractor.py              Ollama + heuristic fallback
├── persona/
│   ├── core.py                   PersonaCore.respond() orchestrator
│   ├── persona_def.py            YAML config loader
│   ├── mood.py                   six-axis + circadian + bad-day
│   ├── prompt_assembler.py       system prompt builder
│   ├── filters.py                AI-ism/em-dash/slop/hedge/sycophancy scrubbers
│   └── llm_router.py             Groq/Ollama/Anthropic with backoff
├── paralinguistics/              M6 scaffolding
├── turn_taking/                  M8/M9 scaffolding
└── voice/                        M0/M1/M5 scaffolding

tests/
├── test_identity.py              4 tests
├── test_persona_config.py        2 tests
├── test_mood.py                  5 tests (including bad-day)
├── test_filters.py               7 tests
├── test_memory.py                4 tests
├── test_metrics.py               3 tests
└── acceptance/
    ├── run_acceptance.py         live-Groq M2/M3/M4 acceptance (8-10 min)
    └── last_run.md               latest report (PASS as of 2026-04-16 23:06)

docs/
├── USAGE.md                      ← start here next session
└── voice_audition_guide.md
```
