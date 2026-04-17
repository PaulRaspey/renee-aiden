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

---

## 2026-04-17 — M7 prosody + M8 turn-taking + M9 backchannel + M10 orchestrator + M11 eval harness

**Context:** Credits-until-dry build session. Text-simulation mode only
(no live audio, no GPU). All five milestones landed green with tests.

**Decisions:**

31. **Prosody hard rule: vulnerable admission always gets sharp_in breath,
    even when blocks-effects is set.**
    - architecture/04_paralinguistics.md says "no paralinguistics during
      disagreement, correction, hard-truth delivery, or user distress"
      and the M6 injector's `blocks_paralinguistics()` drops ALL output
      when any of those flags are set, including mandatory ones. The M7
      brief contradicts this: "Vulnerability always gets (breath in)
      before it, hard rule."
    - Resolution: the vulnerability breath is structural — it IS the
      opening beat of the admission, not ornamentation. The prosody
      layer re-inserts it after the block check, so the injector can
      drop everything and the breath still lands. Other paralinguistics
      stay blocked. Same breath survives the max-per-turn cap.

32. **Sentence pause by mood uses the architecture's period_ms_by_mood
    table exactly; dramatic pre-pauses stack via max, not sum.**
    - `dramatic_before_emotional` (1200ms) and `dramatic_before_callback`
      (300ms) could both trigger on the same turn. Addition produces
      1500ms which is within reason but reads as dramatic + punctuation
      pileup. Using `max()` keeps the larger beat and drops the smaller,
      which matches real cadence.

33. **Heuristic endpointer rather than a small neural model.**
    - architecture/05_turn_taking.md targets a ~100M-param model. We
      don't have one trained, and the consumer API only needs a float
      in [0,1]. Replaced with a piecewise silence ramp plus transcript
      completeness signals (terminal punctuation, comma, continuation
      words, filler tails, short-transcript penalty). Swap for a real
      model later without changing the `decide(...)` surface.

34. **Terminal-punctuation short-transcript exemption.**
    - Tiny complete turns like "Whatever." or "Yeah." should commit.
      Initial rule penalised <3-word transcripts indiscriminately,
      which blocked legitimate one-word closers. Fixed: the short-
      transcript penalty only applies when the transcript doesn't
      already end with `. ! ?`.

35. **Sustain gate on commit (150ms, 100ms tick granularity).**
    - One tick above p=0.9 isn't a commit; a transient spike could fire
      a response while the user's still breathing mid-sentence. Sustain
      timer accumulates on successive high-p ticks and resets on any
      drop. Matches the architecture's "p > 0.9 sustained for 150ms"
      language.

36. **Response latency has a hard floor (80ms) and a clamped jitter
    range (0.75x..1.30x).**
    - The gaussian random multiplier can underflow to zero or go 3x in
      rare tails, which produces either sociopathically fast replies or
      dead air. Clamp in the controller, not the caller.

37. **Renée-interrupts cap uses a rolling window of turn indices, not
    time.**
    - Architecture specifies "max 1 interruption per 10 turns." We count
      turn boundaries via `on_turn_boundary()` and maintain a deque of
      interrupt-turn indices. A quiet hour shouldn't let the cap
      "recharge"; a chatty 10-turn burst keeps the cap pressed down.

38. **Backchannel layer fires at -6dB via config default; rate caps
    (min gap, max/minute) are orthogonal from trigger probability.**
    - Architecture calls for -6dB mix and rate scaling with warmth.
      Implemented trigger probability as a multiplicative stack
      (base × warmth × intimacy × tone × trigger_type). Rate caps are
      gate checks before probability math so we never roll when we're
      about to drop anyway. Keeps the probability math interpretable.

39. **Hard block on backchannel during disagreement / distress / heated.**
    - architecture/04 and /05 both call this out. The block is cheap
      and absolute: the layer returns 0.0 before doing any work. Users
      will not tolerate mhm-ing while they're falling apart.

40. **Orchestrator classifier is heuristic; the seam is intentional.**
    - `TurnClassifier.classify(user_text, response_text, mood)` is crude
      string matching. M11 proves we can measure the result; a small
      LLM classifier can drop in later without touching the
      orchestrator. Keeping it heuristic now means text-sim tests run
      in milliseconds.

41. **Per-layer telemetry as JSONL at state/orchestrator.jsonl.**
    - MetricsStore already stores per-turn totals for the persona core.
      Orchestrator writes a richer per-turn line with per-layer
      breakdowns (persona_respond_ms, injector_plan_ms, prosody_plan_ms,
      classify_ms) plus the classified context and paralinguistic_count.
      JSONL keeps dashboard reads cheap and tailing trivial.

42. **Eval scorers are stateless heuristic functions, not an LLM judge.**
    - An LLM-as-judge pass would be cleaner for `emotional_congruence`
      and `pushback` but adds token cost to every probe run. For M11
      alpha, heuristic suffices: they already cover the architecture's
      seven measurable axes (hedge_rate, sycophancy, ai-isms, length,
      callback_hit, emotional_congruence, pushback) plus
      opinion_consistency for persona-aware checks.

43. **A/B queue random-swaps labels on queue, not on read.**
    - Rater can't tell which side is the candidate. Swap happens inside
      `queue_pair`, stored to SQLite as `label_a` / `label_b`. Keeps
      the read path trivial: no need to re-seed an RNG per fetch.

44. **Style extractor runs on the original reference script
    (scripts/renee_reference_script.md), NOT on *Her*.**
    - PJ wrote a richly annotated reference script as original work.
      Architecture decision #3 forbids training on *Her*; the extractor
      honors that by only touching the original script. M12 proper
      (Her analysis) remains a separate milestone with its own
      copyright review.

45. **Dashboard renders offline from SQLite + JSONL, no JS framework.**
    - architecture/06 asked for `localhost:7860/eval`. The nightly run
      just dumps a self-contained `eval_dashboard.html`. If PJ wants
      a live HTTP server later, `python -m http.server` on the state
      dir gets him the same URL. Keeps the eval surface one `open`
      command away without a server dependency.

---

## 2026-04-17 — M12 style expansion + M13 safety layer + M14 cloud skeleton

**Context:** Credits-until-dry continuation. Three milestones plus a
Groq filter fix in one session. No live audio or GPU still.

**Decisions:**

46. **ip_reminder leaks killed in the output filter, not the LLM prompt.**
    - Qwen on Groq occasionally emits `<ip_reminder>...</ip_reminder>`
      system-style tags (similar vintage as the `<think>` leak we already
      strip). Teaching the model to stop at prompt time is unreliable
      across model upgrades. The deterministic fix is to scrub at the
      filter pipeline — closed form, orphan opener/closer, and prose
      `ip_reminder: ...` line variant. Logged as `ip_reminder` in
      `FilterReport.hits` so we can trend the leak rate in eval.

47. **M12 callback detector indexes BOTH speakers' tokens.**
    - Renée recalls Florence and Marcus from scenes Paul originally
      introduced. Indexing only Renée's tokens would have missed those
      callbacks entirely. The extractor now pools capitalized tokens
      across both speakers per scene, filters a manually-curated stop
      list of exclamations / connectives, and reports a `renee_callbacks`
      list showing which scenes she recalls from prior scenes where she
      didn't originate the anchor. Keeps the call-graph interpretable
      for future prompt feedback loops.

48. **Scene mood labels are heuristic, not LLM-judged.**
    - `_scene_mood_label` in `style_extractor` maps marker counts to
      one of `{light, casual, serious, intimate, conflict}`. An LLM
      classifier would be stricter but ties the extractor to an API
      call per scene and makes the result non-deterministic. The
      heuristic mislabels nothing on the current 10-scene reference
      script (spot-checked scene 8 -> serious, scene 4 -> intimate,
      scene 9 -> intimate), so we ship it; a small-model classifier
      can drop in later without touching consumers.

49. **Style reference flows into two places, not one.**
    - Prompt-side: `build_system_prompt` injects a STYLE CONSTRAINTS
      block so the LLM sees measured targets (median turn length, hedge
      rate, paralinguistic density, signature phrases, known callback
      anchors) — not free-form style advice, concrete numbers.
    - Prosody-side: `ProsodyPlanner` absorbs the per-tone paralinguistic
      density derived from mood_arc and overrides the rule-table value
      for tones present in the reference. Tones not observed in the
      script keep the YAML default. This keeps the rule engine the
      source of truth but lets measured data correct it.

50. **M13 PII scrubbing runs in PersonaCore, not at the LLM router.**
    - Bracketing the scrub around `router.generate(...)` means the
      router stays generic. But we also scrub the system prompt and
      history — and only PersonaCore has a full view of what's going
      into the model, especially once the eval harness adds additional
      system blocks. Doing it at PersonaCore also means mocked routers
      in tests don't accidentally scrub their fixtures.

51. **Reality anchor fires AFTER output filters, BEFORE mood update.**
    - Anchors shouldn't be subject to the hedge/sycophancy regen loop
      (they're honest meta-commentary, not claims). They should
      influence the turn Renée actually speaks. Injecting after filters
      but before the mood update keeps them visible to the user and the
      subsequent `MoodStore.apply_tone` step (which now sees any anchor
      text in `report.text`).

52. **HealthMonitor flags look at last N FULL days, not a trailing window
    that includes today.**
    - Including a partially-elapsed day makes thresholds flicker as
      minutes accumulate through the day. The check looks at
      `rolling_daily_minutes(N+1)[:-1]` — the N completed days before
      today — and demands all of them clear the threshold. Matches the
      intent in SAFETY.md ("sustained 2 weeks" = 14 completed days).

53. **Memory encryption off by default, but the machinery ships.**
    - AES-256-GCM via `cryptography` is in-tree (`MemoryVault`,
      `derive_key`) and tested. We don't flip it on yet because the
      existing memory tests exercise plaintext paths and PJ hasn't
      picked a keyring posture. When he does, it's `safety.yaml
      memory_encryption.enabled: true` and a `MemoryVault` around the
      memory DB reads/writes.

54. **Keyring is optional; file-scoped fallback always exists.**
    - `derive_key` tries `keyring` first, then falls back to a state-dir
      key file. Both paths yield a 32-byte AES-256 key; the caller
      can't tell the difference. If keyring became available later
      after the fallback fired, `derive_key` stashes the existing key
      into keyring for next time, so the user gets progressive
      security without a migration.

55. **M14 cloud components are skeletons with factory-injected tests.**
    - We can't actually run WebSocket + Opus + RunPod from PJ's dev
      box. The pattern: all heavy deps import lazily inside functions,
      the orchestration layer accepts factory callables for
      orchestrator/bridge/idle watcher, and tests inject fakes. The
      shape is fully exercised (phase ordering, error tracking, pod
      lifecycle, idle watcher semantics) without touching the network.
      When we spin up RunPod, installing `websockets` / `opuslib` /
      `sounddevice` / `runpod` is all that's needed.

56. **`python -m renee` via a thin wrapper package aliasing src.**
    - The CLI lives in `src.cli.main`. Rather than rename `src/` to
      `renee/` (invasive: every import in every file) we added
      `renee/__init__.py` + `renee/__main__.py` that re-export the
      dispatcher. Both `python -m renee` and `python -m src` work; the
      architecture doc's `python -m renee wake` lands as expected.

57. **Idle watcher latches after firing; mark_activity rearms.**
    - A one-shot latch prevents `tick()` from firing shutdown multiple
      times during a long idle stretch. Any subsequent `mark_activity`
      (e.g. a late frame that slipped in while the pod is winding down)
      rearms the watcher. Matches the spirit of "60 min idle → graceful
      shutdown, not a shutdown storm."

58. **Export command copies from the state dir, not a manifest.**
    - `python -m renee export --output ...` walks `state/` and mirrors
      every file into the output directory. No manifest, no
      transformation — the caller gets a byte-for-byte copy that
      `scripts/volume_setup.py` or a future `import` command can feed
      back into a fresh pod. Keeps the round-trip story honest: what
      you export is exactly what's on disk.
