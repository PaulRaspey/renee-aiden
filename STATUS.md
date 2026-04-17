# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** M0, M2, M3, M4, M5, M6 green. XTTS-v2 model load is the only remaining M5/M6 gap; needs GPU.
**Branch:** main
**Repo:** https://github.com/PaulRaspey/renee-aiden (private)
**Last commit:** `M5 reference corpus + M6 injection engine + library generator`
**Next milestone:** M7 prosody layer (needs XTTS-v2 on GPU) or M1 ASR
**Blockers:** None for M5/M6 scaffolding. XTTS-v2 model load needs a CUDA GPU (RunPod H100 spin-up).

## How to resume

1. `cd C:\Users\Epsar\Desktop\renee-aiden`
2. `.venv\Scripts\activate`
3. `python -m pytest tests/ --ignore=tests/acceptance` (49 tests, ~20s)
4. Read `docs/USAGE.md` for CLI. For voice: see `scripts/generate_reference_corpus.py` and `scripts/generate_paralinguistic_library.py`.
5. Rotate the ElevenLabs key — it was pasted in chat. Update `.env` after rotation.

## What's done

- [x] Architecture spec + 8 stack deep dives (pre-session)
- [x] Git repo, pushed to GitHub, private
- [x] `src/identity/` — UAHP-native agent keys, signed receipts (HMAC-SHA256)
- [x] `src/persona/` — persona def, mood, prompt assembler, output filters,
      dual-backend LLM router
- [x] `src/memory/` — SQLite + FAISS, tier-weighted retrieval
- [x] `src/eval/` — turn telemetry store, report CLI, humanness probe runner
- [x] `src/cli/chat.py` — REPL
- [x] **M5: reference voice corpus** — `scripts/generate_reference_corpus.py`,
      88 WAVs across 9 emotional registers in `voices/renee/reference_clips/`
      (~15 min total at 24 kHz mono). Resumable, uses ElevenLabs voice
      `h8pr4vZSN32hZy70aZCN` (Renée).
- [x] **M6: injection engine** — `src/paralinguistics/injector.py`
      with mandatory/ornamental rule split, dedup, mood filter, frequency cap,
      recency filter, hard rule blocking during disagreement/correction/hard
      truth/user distress/heated tone. 18 unit tests.
- [x] **M6: library generator** — `scripts/generate_paralinguistic_library.py`
      with 24 (category, subcategory) specs. Carrier-text + audio tags + longest
      non-silent segment isolation. Resumable.
- [x] **XTTS-v2 loader scaffold** — `src/voice/xtts_loader.py`: `preflight()`,
      `reference_wavs()`, and a `load()` that raises NotImplementedError until
      CUDA shows up. 6 unit tests.
- [x] `tests/` — 49 unit tests passing.

- [x] **M6: library generated** — 3,600 WAV clips (150 × 24 categories) in
      `paralinguistics/renee/`. Total ~47.3 min of paralinguistic audio.
      Metadata at `paralinguistics/renee/metadata.yaml`.

## What's next (rough order)
- [ ] M1 ASR — needs faster-whisper; install audio deps when voice comes back
- [ ] M7 prosody control — needs M5 done + XTTS on GPU
- [ ] M8 turn-taking + endpointer
- [ ] M9 backchannel
- [ ] M10 end-to-end voice integration
- [ ] M11 full eval harness w/ dashboard
- [ ] M12 *Her* script analysis
- [ ] M13 safety layer (PII scrubber, relationship-health monitor)
- [ ] M14 cloud deployment to RunPod
- [ ] M15 long-running test

## Known risks / gotchas (new this session)

- **pcm_44100 unavailable on current ElevenLabs plan.** Fell back to pcm_24000
  for every generation. XTTS-v2 is native 24 kHz so this is actually fine, but
  the architecture doc asked for 48 kHz. Upgrade to Creator/Pro tier if you
  want 44.1 or 48 kHz archival quality.
- **ElevenLabs rejects tag-only prompts.** `[laughs softly]` alone returns
  400 input_text_empty. All paralinguistic prompts include at least one
  carrier syllable (ha, mm, yeah, oh, hm) so the API accepts them. The
  `isolate_paralinguistic()` function in `generate_paralinguistic_library.py`
  keeps only the longest non-silent segment, which should drop the carrier
  syllable in most clips. Spot-check a handful before using.
- **ElevenLabs returns sporadic 500s.** `el_client.generate_pcm()` retries
  5xx with exponential backoff (up to 6 attempts). The first M5 run crashed
  on a raw 500 before retry was hardened; resume worked fine after.
- **eleven_v3 model tagging.** We use eleven_v3 for laughs/sighs/breaths/reactions
  and eleven_multilingual_v2 for plain-word categories (mm, hmm, yeah, fillers).
  If eleven_v3 access is revoked, the client falls back to
  eleven_multilingual_v2 automatically but expressive tags will degrade.
- **ElevenLabs API key was pasted in chat** (sk_78a455…). Rotate after
  session. Same pattern as your GitHub PATs: revoke after "I am going to bed now".
- **Audio packages installed this session:** `soundfile`, `librosa`,
  `pyloudnorm`, `pydub`, `elevenlabs`. See `requirements.txt`.
  Still NOT installed: `sounddevice`, `webrtcvad`, `opuslib`, `faster-whisper`, `TTS` (Coqui).
  Coqui TTS only installs on the GPU pod.

## Code at a glance

```
src/
├── __init__.py
├── cli/chat.py
├── eval/
├── identity/
├── memory/
├── paralinguistics/
│   ├── __init__.py              exports ParalinguisticInjector, TurnContext, ...
│   └── injector.py              M6 rule engine + clip selector
├── persona/
├── turn_taking/
└── voice/
    ├── __init__.py
    └── xtts_loader.py           preflight + reference_wavs + GPU load stub

scripts/
├── bootstrap.py
├── chat.bat
├── el_client.py                 shared ElevenLabs REST helper
├── generate_reference_corpus.py M5
└── generate_paralinguistic_library.py M6

voices/
└── renee/
    ├── metadata.yaml            88 clips across 9 registers
    └── reference_clips/         neutral_01..18, warm_01..12, ...

paralinguistics/
└── renee/
    ├── metadata.yaml            grows as M6 run progresses
    ├── laughs/{soft,hearty,suppressed,nervous}/
    ├── sighs/{content,frustrated,tired,thinking}/
    ├── breaths/{sharp_in,slow_out,thinking}/
    ├── thinking/{mm,hmm,uh,oh}/
    ├── affirmations/{yeah,right,mhm}/
    ├── reactions/{surprise,amusement,ugh}/
    └── fillers/{you_know,i_mean,like}/

tests/
├── test_identity.py              4 tests
├── test_persona_config.py        2 tests
├── test_mood.py                  5 tests
├── test_filters.py               7 tests
├── test_memory.py                4 tests
├── test_metrics.py               3 tests
├── test_paralinguistic_injector.py  18 tests   ← new
├── test_xtts_loader.py              6 tests    ← new
└── acceptance/
    └── run_acceptance.py         live-Groq M2/M3/M4 acceptance
```
