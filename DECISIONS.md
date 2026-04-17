# Decisions Log

Append-only. Each decision logged with: date, context, options, choice, rationale.

---

## 2026-04-16 — Initial architecture

**Context:** Building Renée/Aiden from scratch on the UAHP protocol stack.

**Decisions:**

1. **XTTS-v2 as primary TTS, Chatterbox fallback**
   - Options: XTTS-v2, Chatterbox, ElevenLabs API, OpenVoice
   - Chose XTTS-v2 for emotional range + local deployment. Chatterbox for fast simple turns. ElevenLabs API only for reference voice creation, not runtime.

2. **Groq Qwen 3 32B as primary LLM, Claude Sonnet 4.6 for deep turns, Gemma local for fast**
   - Options: Claude-only, Groq-only, self-hosted-only, hybrid
   - Chose hybrid. Cost + latency + quality all matter. Router decides per turn.

3. **Never train on *Her* audio**
   - Options: Use as training data, use as reference, ignore entirely
   - Chose reference-only. Legal risk too high. Style extraction from script text is fine.

4. **Separate Renée and Aiden from the start**
   - Options: One codebase with persona swap, two codebases, one codebase with shared architecture
   - Chose third option. Shared architecture, separate configs, separate state, separate voices, separate memory.

5. **Windows-first development, CMD-compatible**
   - Options: WSL, Linux-only, Windows-native
   - Chose Windows-native. PJ's target environment. Revisit for production hardware.

6. **UAHP-native from day one**
   - Options: Build without UAHP, retrofit UAHP later, UAHP-native
   - Chose UAHP-native. PJ's existing protocol stack. Cryptographic identity, trust, receipts all free.

7. **Paralinguistic library approach: recorded clips + runtime injection**
   - Options: Pure TTS with prosody tricks, recorded clips, fine-tuned TTS on paralinguistic corpus
   - Chose clips + injection. Best fidelity per dollar, works with existing XTTS-v2, recorded once used forever.

8. **Variable response latency based on turn type**
   - Options: Fixed low latency, fixed realistic latency, variable
   - Chose variable. Hard-coded latency on emotional content is the #1 tell for AI.

9. **Memory: emotionally-weighted retrieval with tiers**
   - Options: Pure RAG, episodic memory with tiers, summarization-based
   - Chose tiers + weighted retrieval. Matches how humans actually remember relationships.

10. **Separate evaluation harness from production system**
    - Options: Inline eval, separate harness, none
    - Chose separate. Nightly runs. Metrics trend over time. Regression detection.

---

## 2026-04-16 — Implementation session (M0 through M4)

**Context:** Claude Code session picking up after handoff. PJ asked to skip the M0 audio I/O round-trip and focus on the text conversation loop through M4. Environment: Windows 11 CMD, Python 3.12.10, venv at `.venv`, Groq key at `~/.bridge_key`, dual T400 GPUs (4GB each), Ollama v0.20.4 with gemma3:4b available.

**Decisions:**

11. **Drop the broken PyPI `uahp` package, re-implement identity primitives in-repo**
    - The `uahp==0.5.4` wheel imports `.identity`, `.capability`, `.intent`, `.session`, `.verification`, `.canon`, `.enums`, `.schemas` at init time — none of those modules ship in the wheel, so `import uahp` raises `ModuleNotFoundError`.
    - Options: pin older version (none available), vendor from PJ's `uahp-stack/` repo, implement locally.
    - Chose to implement `src/identity/uahp_identity.py` directly from the pattern in PJ's `uahp-stack/core.py`. HMAC-SHA256 signing, SHA-256 public hash, keyed by `agent_id`. Keeps the Renée repo standalone. Full agent handshake, liveness, and death-certificate flow can be added when voice orchestration needs them in M8-M10.
    - If the PyPI wheel is fixed later, swap the implementation without touching the public API (`sign_receipt`, `verify_receipt`, `ReneeIdentityManager.get`).

12. **Python 3.12 instead of 3.11**
    - The handoff doc asked for 3.11, the environment is 3.12.10. All deps (uahp, faiss-cpu, sentence-transformers, groq, ollama) install cleanly on 3.12. Kept 3.12. Will revisit if a cloud/production pod requires 3.11.

13. **Skip the audio subsystem packages for this session**
    - PJ asked to focus on text. `sounddevice`, `soundfile`, `webrtcvad`, `opuslib`, `librosa`, `pyloudnorm`, `faster-whisper`, `TTS` (Coqui) were NOT installed this session. Only core deps for M0-M4 were installed. Added to the pip install list in the next session that touches M1/M5.

14. **Keep the BOM-tolerant bridge-key parser**
    - The `~/.bridge_key` file on this machine has a UTF-8 BOM (`\xef\xbb\xbf`) from Notepad. Added `utf-8-sig` decoding + ASCII-safe header sanitation in `LLMRouter._read_bridge_key` so Windows-edited key files work without hand-stripping.

15. **`qwen/qwen3-32b` with `reasoning_effort="none"`**
    - Qwen 3 leaks internal `<think>...</think>` blocks by default on Groq. Passing `reasoning_effort="none"` keeps the voice. The filter also strips `<think>` blocks defensively.

16. **Default fast-backend model is `gemma3:4b`, not `gemma2:2b`**
    - PJ's environment has gemma3:4b loaded in Ollama. Overrides via `OLLAMA_MODEL` env var; the bootstrap script still pulls `gemma2:2b` as a minimal fallback.

17. **Windows `src/{a,b,c}` directory was a CMD brace-expansion artifact**
    - The previous session ran `mkdir src/{voice,persona,...}` on Windows CMD which doesn't expand braces, creating a single literal directory. Removed and recreated the six subdirs plus `src/identity` and `src/cli`.

18. **`configs/renee.yaml` had an unquoted colon inside a list item ("Reality anchors allowed: ...")**
    - YAML parser was treating the colon as a mapping key. Wrapped that one line in single quotes. No semantic change.

19. **Core facts kept in `src/cli/chat.py` rather than a yaml**
    - For M2/M3, the CORE-tier seed facts about PJ are a Python list in the CLI, injected both into the system prompt and seeded into the memory store on first run. Move to `configs/pj_facts.yaml` when we support multiple subject identities.

20. **Sensitive-tier memories are hard-filtered, not just zero-weighted**
    - Previously scored 0.0 on tier weight but could still reach top-k via the salience floor. Now they're explicitly dropped unless `user_raised_sensitive=True`. Matches the design intent in `architecture/03_memory.md`: "She knows. She won't raise them."

---

## 2026-04-17 — M5 reference corpus + M6 paralinguistics

**Context:** PJ picked Renée's voice on ElevenLabs (id `h8pr4vZSN32hZy70aZCN`) and asked to build M5 + M6 end to end on his paid ElevenLabs plan. XTTS-v2 model load deferred (no local GPU). Built through model-load point so RunPod spin-up can "load and go."

**Decisions:**

21. **ElevenLabs as M5 reference-corpus generator, not just for voice design**
    - architecture/01_voice.md mentions ElevenLabs for reference-voice creation.
      Used it end-to-end: 88 WAVs across 9 emotional registers in
      `voices/renee/reference_clips/`. Dialogue scripts are in-script in
      `scripts/generate_reference_corpus.py` rather than YAML: one source of
      truth, easy to diff and revise.

22. **pcm_24000 as the canonical output format**
    - pcm_44100 returned a plan error on the first call; fallback chain shipped
      with el_client picked pcm_24000. Left pcm_24000 as the default because
      XTTS-v2 is native 24 kHz so we avoid resampling. If we later want 48 kHz
      archival quality, bump the ElevenLabs plan and reintroduce pcm_44100.

23. **Carrier syllables on every paralinguistic prompt**
    - ElevenLabs rejects tag-only text (`input_text_empty` 400). Every
      paralinguistic prompt now includes at least one non-tag syllable (ha,
      mm, yeah, oh, hm). Isolation is done post-hoc by keeping the longest
      non-silent segment via `librosa.effects.split` — the paralinguistic is
      almost always longer than the filler syllable.

24. **eleven_v3 for expressive tags, eleven_multilingual_v2 for plain words**
    - Laughs/sighs/breaths/reactions run through eleven_v3 where the inline
      tags actually shape the output. Word-based categories (mm, hmm, yeah,
      right, mhm, fillers) run through eleven_multilingual_v2 because it's
      more stable on short plain text. el_client falls back v3 → v2 on
      `model_not_found`, so the library degrades gracefully rather than
      refusing to run.

25. **Mandatory vs ornamental split in the injector**
    - Original design from architecture/04_paralinguistics.md put density
      tuning ahead of rule evaluation, which meant a vulnerable-admission
      could lose its sharp inhale to a random density roll. Split the rule
      engine into `_propose_mandatory` (semantic signals that always fire
      when their triggers are on: vulnerable admission, high complexity,
      repeated confusion + low patience) and `_propose_ornamental` (stylistic
      flourishes subject to the density gate). The architecture doc's density
      table still governs ornamentation; semantic injections are load-bearing
      and bypass it.

26. **Resumability over batch integrity**
    - Both generator scripts skip files that already exist on disk. This makes
      the 3,600-clip M6 run tolerant to transient ElevenLabs 500s (which do
      happen). metadata.yaml is rewritten after each category so a mid-run
      crash leaves a consistent index for everything done so far. Cost: a
      resumed run's metadata has `prompt: null` for pre-existing clips; we
      accept that because the clip itself and its category/subcategory are
      what the injector actually consumes.

27. **XTTS-v2 loader split into local-safe and GPU-only halves**
    - `preflight()` and `reference_wavs()` run locally — verify the corpus
      and pick reference clips from it. `load()` imports `torch` + Coqui
      `TTS` and raises `NotImplementedError` when CUDA isn't available. This
      lets us test the pre-load plumbing on PJ's T400 box without touching
      the big dependencies.

28. **`.env` tracks the ElevenLabs key, not a hardcoded secret**
    - Key was pasted in chat, so PJ should rotate; in the meantime it lives
      in `.env` (gitignored). Same exfil posture as his `GROQ_API_KEY`.
      `RENEE_VOICE_ID` also lives there so the generator is a single
      `python scripts/generate_reference_corpus.py` invocation.

29. **Library generator always indexes all categories**
    - First cut of `--only` overwrote `metadata.yaml` with just the selected
      category's entries. Fixed by always iterating every category: selected
      ones generate + index, unselected ones harvest existing WAVs via
      `_harvest_existing` so `--only` can never drop entries from the index.
      Makes the metadata a pure function of what's on disk.

30. **Accept 47.3 min of paralinguistic audio across 24 × 150 clips**
    - PJ's floor was 150 per category; hit exactly that. One category
      (reactions/surprise) needed a backfill because a tag-only prompt
      (`[surprised gasp]`) kept tripping the empty-text validator. Rewrote
      to `Oh [surprised gasp].` and backfilled. Metadata rebuild confirms
      3,600 clips across all 24 subcategories.
