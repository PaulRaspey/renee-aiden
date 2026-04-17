# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** M0, M2–M14 green. M1 ASR + M10 live audio still need a CUDA GPU / audio deps.
**Branch:** main
**Repo:** https://github.com/PaulRaspey/renee-aiden (private)
**Last commit:** `M14 cloud deployment: startup + bridge + pod manager + CLI`
**Next milestone:** M15 long-running test (or install audio deps for live M0/M1)
**Blockers:** None for text-mode. Live audio still needs CUDA for XTTS-v2 and the
OptiPlex thin client needs `sounddevice` / `opuslib` / `websockets` / `runpod`
installed locally.

**Test summary:** 298 tests passing. 4 pre-existing memory tests fail on
HuggingFace network access only.

## How to resume

1. `cd C:\Users\Epsar\Desktop\renee-aiden`
2. `.venv\Scripts\activate`
3. `python -m pytest tests/ --ignore=tests/acceptance --ignore=tests/test_memory.py`
4. `python -m renee` to see CLI surface. `python -m renee text` drops into the M2 REPL.
5. Read `docs/USAGE.md` for earlier CLI. For voice: `scripts/generate_reference_corpus.py`
   and `scripts/generate_paralinguistic_library.py`.
6. Rotate the ElevenLabs key — it was pasted in chat. Update `.env` after rotation.

## What's done

- [x] Architecture spec + 9 stack deep dives (including cloud deployment)
- [x] Git repo, pushed to GitHub, private
- [x] M0: scaffolding, UAHP identity, first tests
- [x] M2/M3/M4: persona core, mood, memory, text chat REPL
- [x] M5: reference voice corpus — 88 WAVs across 9 emotional registers
- [x] M6: paralinguistic injector + library generator (3,600 clips, 47.3 min)
- [x] M7: prosody layer (rate/pitch/pauses/effects, vulnerable-admission hard rule)
- [x] M8: turn-taking (endpointer, latency, interruption)
- [x] M9: backchannel layer
- [x] M10: orchestrator (wires persona, paralinguistics, prosody, turn-taking)
- [x] M11: eval harness (scorers, A/B, callbacks, style extractor, dashboard)
- [x] **M12: expanded style extractor + persona/prosody integration**
      - Scene-aware parsing; per-scene paralinguistic density and mood label
      - Turn-length percentiles (p25/50/75/90/95/99)
      - Callback graph (cross-scene anchors, Renée-owned recalls)
      - Vocabulary texture (type/token ratio, top content words,
        signature-phrase hits, sensory density)
      - Pause distribution breakdown
      - `src/persona/style_rules.py` loads the YAML into a `StyleReference`
      - Persona prompt auto-injects STYLE CONSTRAINTS block with measured
        targets; prosody planner absorbs measured per-tone paralinguistic
        density and overrides the default density rules.
- [x] **M13: safety layer**
      - `src/safety/reality_anchors.py` — probabilistic reality anchors
        with suppress flags + min-turn-gap, deterministic under RNG seed.
      - `src/safety/health_monitor.py` — SQLite-backed daily minutes
        aggregator, soft + stronger flags on sustained-days thresholds
        with cooldown.
      - `src/safety/pii_scrubber.py` — regex + name-boundary scrubber;
        tokens <USER>/<CHILD_N>/<ADDRESS_N>/<SENSITIVE_N>/<EMAIL_N>/
        <PHONE_N>. Round-trip unscrub with longest-token-first ordering.
      - `src/safety/memory_crypto.py` — AES-256-GCM encrypt/decrypt with
        magic header; `MemoryVault` path-scoped wrapper; key derivation
        prefers keyring, falls back to state-dir keyfile.
      - `configs/safety.yaml` ships with thresholds mirroring SAFETY.md.
      - PersonaCore wires the safety layer: PII scrub pre-LLM, unscrub on
        response path, reality anchor between filters and mood update,
        health record at end of turn.
- [x] **M14: cloud deployment skeleton**
      - `scripts/cloud_startup.py` — 7-phase boot orchestrator (health,
        UAHP, parallel model load, agent register, state restore, bridge,
        self-test) with factory injection for testability.
      - `src/server/audio_bridge.py` — WebSocket + Opus bridge shell on
        the cloud side; lazy imports so module loads without audio deps.
      - `src/server/idle_watcher.py` — pluggable-clock idle watcher with
        one-shot fire + rearm-on-activity semantics.
      - `src/client/audio_bridge.py` — OptiPlex thin client; mic capture
        via sounddevice, opus encode/decode, WebSocket.
      - `src/client/pod_manager.py` — RunPod lifecycle (wake / sleep /
        status); `DeploymentSettings` parses `configs/deployment.yaml`.
      - `src/cli/main.py` — argparse dispatcher; subcommands `wake`,
        `talk`, `sleep`, `status`, `text`, `eval`, `export`.
      - `src/__main__.py` + `renee/__main__.py` give both
        `python -m src` and `python -m renee`.
- [x] **ip_reminder fix** — Groq/Qwen occasionally leaks `<ip_reminder>`
      system tags. Stripped in the output filter pipeline (closed, orphan,
      and prose line forms); logged as `ip_reminder` in filter hits.

## What's next

- [ ] Install audio deps (`sounddevice`, `webrtcvad`, `opuslib`, `faster-whisper`,
      `websockets`, `runpod`) for live M0/M1/M14 runs.
- [ ] First RunPod spin-up: run `scripts/volume_setup.py` to populate the
      network volume, then `python -m renee wake`.
- [ ] M15 long-running test — overnight conversation session with eval
      dashboard snapshots every hour.
- [ ] Revisit memory encryption once PJ's key-storage story is settled.
- [ ] Hook the A/B queue into the CLI so PJ can rate pairs without leaving
      the terminal.

## Known risks / gotchas

- **pcm_44100 unavailable on current ElevenLabs plan.** Using pcm_24000.
  XTTS-v2 is native 24 kHz so this is fine; upgrade to Creator/Pro tier
  only if 44.1 or 48 kHz archival quality is required.
- **ElevenLabs rejects tag-only prompts.** Carrier syllables added.
- **ElevenLabs returns sporadic 500s.** Client retries 5xx with exponential
  backoff.
- **eleven_v3 model tagging.** v3 for expressive tags, v2 for plain words.
- **ElevenLabs API key was pasted in chat** (sk_78a455…). Rotate after
  session.
- **Qwen-on-Groq leaks ip_reminder tags.** Fixed in the filter, but if you
  swap models double-check.
- **Memory encryption off by default.** `MemoryVault` exists but isn't
  wired into the SQLite memory store yet — flip
  `configs/safety.yaml memory_encryption.enabled` when ready.
- **Audio packages installed this session:** prior (M5-M6) — `soundfile`,
  `librosa`, `pyloudnorm`, `pydub`, `elevenlabs`. Still NOT installed:
  `sounddevice`, `webrtcvad`, `opuslib`, `faster-whisper`, `TTS` (Coqui),
  `websockets`, `runpod`. `cryptography` is installed (46.0.7) and now
  pinned in `requirements.txt`.

## Code at a glance

```
src/
├── __init__.py
├── __main__.py                  python -m src dispatcher
├── cli/
│   ├── chat.py                  M2 REPL
│   └── main.py                  M14 argparse dispatcher
├── client/
│   ├── audio_bridge.py          OptiPlex thin-client
│   └── pod_manager.py           RunPod lifecycle + config
├── server/
│   ├── audio_bridge.py          cloud-side WebSocket + Opus bridge
│   └── idle_watcher.py          idle auto-shutdown
├── eval/
│   ├── ab.py                    blind A/B queue
│   ├── callbacks.py             callback hit tracker
│   ├── dashboard.py             single-file HTML dashboard
│   ├── harness.py               probe runner
│   ├── metrics.py               turn telemetry store
│   ├── probes.py                probe configs
│   ├── report.py                eval report CLI
│   ├── scorers.py               8 humanness axes
│   └── style_extractor.py       M11+M12 script analyzer
├── identity/
│   └── uahp_identity.py         HMAC-SHA256 signed receipts
├── memory/                      SQLite + FAISS tier-weighted retrieval
├── orchestrator.py              M10 top-level pipeline
├── paralinguistics/
│   └── injector.py              M6 rule engine + clip selector
├── persona/
│   ├── core.py                  LLM turn + mood + filters + safety
│   ├── filters.py               output scrubber (ip_reminder-aware)
│   ├── llm_router.py            Groq / Ollama / Anthropic router
│   ├── mood.py                  mood store + drift
│   ├── persona_def.py           YAML loader
│   ├── prompt_assembler.py      system prompt builder (style-aware)
│   └── style_rules.py           M12 style reference loader
├── safety/                      M13
│   ├── config.py                SafetyConfig loader
│   ├── facade.py                SafetyLayer composition
│   ├── health_monitor.py        daily-minutes aggregator + flags
│   ├── memory_crypto.py         AES-256-GCM + key derivation
│   ├── pii_scrubber.py          CSP-style tokenizer
│   └── reality_anchors.py       ~1-in-50 anchor injector
├── turn_taking/                 M8-M9
└── voice/
    ├── prosody.py               M7 planner (style-aware)
    └── xtts_loader.py           GPU load stub

renee/                           python -m renee wrapper
├── __init__.py
└── __main__.py

scripts/
├── bootstrap.py
├── chat.bat
├── cloud_startup.py             M14 RunPod boot
├── el_client.py
├── generate_paralinguistic_library.py
├── generate_reference_corpus.py
└── renee_reference_script.md

configs/
├── aiden.yaml
├── deployment.yaml
├── humanness_probes.yaml
├── prosody_rules.yaml
├── renee.yaml
├── safety.yaml                  M13
└── style_reference.yaml         auto-generated, M11+M12

tests/                           298 passing
├── acceptance/
├── test_audio_bridges_smoke.py
├── test_backchannel.py
├── test_cli_main.py
├── test_client_pod_manager.py
├── test_cloud_startup.py
├── test_eval_ab.py
├── test_eval_callbacks.py
├── test_eval_harness.py
├── test_eval_scorers.py
├── test_eval_style_extractor.py
├── test_filters.py
├── test_identity.py
├── test_memory.py               (HF-network dependent; skipped by default)
├── test_metrics.py
├── test_mood.py
├── test_orchestrator.py
├── test_paralinguistic_injector.py
├── test_persona_config.py
├── test_prosody.py
├── test_safety_crypto.py
├── test_safety_facade.py
├── test_safety_health.py
├── test_safety_pii.py
├── test_safety_reality_anchors.py
├── test_server_idle_watcher.py
├── test_style_rules.py
├── test_turn_taking.py
└── test_xtts_loader.py
```
