# Build Order

Claude Code: work through this list in order. Each milestone has acceptance criteria. Do not proceed to the next milestone until the current one passes. Commit after each milestone.

---

## M0 — Foundation (half day)

**Goal:** Project scaffolding, UAHP integration, basic audio I/O loop.

- [ ] Initialize git repo, push to `github.com/PaulRaspey/renee-aiden` (private)
- [ ] Set up Python 3.11 venv, pin dependencies in `requirements.txt`
- [ ] Install UAHP from PyPI: `pip install uahp>=0.5.4`
- [ ] Wire basic UAHP identity for Renée and Aiden (Ed25519 keypairs, registered to local UAHP-Registry)
- [ ] Implement `src/voice/audio_io.py` — mic capture with WebRTC VAD, speaker output via sounddevice
- [ ] Round-trip test: speak into mic, see transcribed text, hear playback of fixed string

**Acceptance:** Run `python -m renee.loopback`, speak "test", see transcription in console, hear "acknowledged" played back. Round trip under 2 seconds end-to-end.

---

## M1 — ASR Layer (half day)

**Goal:** Streaming speech-to-text that emits partial transcripts.

- [ ] Integrate `faster-whisper` with Large-v3-Turbo model
- [ ] Implement streaming wrapper that emits partial transcripts every 300ms
- [ ] Wrap as UAHP agent `asr_agent` with signed output receipts
- [ ] Support configurable model size for hardware scaling

**Acceptance:** Partial transcripts stream within 500ms of speech onset. Final transcript accuracy >95% on clear speech. Completion receipts signed and verifiable.

---

## M2 — Persona Core, Text-Only (one day)

**Goal:** The brain works in text before we touch voice.

- [ ] Implement `src/persona/core.py` with dual-backend routing (Groq Qwen 3 32B for deep, local Gemma for fast)
- [ ] Load persona definition from `configs/renee.yaml` and `configs/aiden.yaml`
- [ ] Implement system prompt assembly from: base persona + current mood + recent memories + conversation context
- [ ] Hedging enforcement: post-process to ensure >30% of factual statements include uncertainty markers
- [ ] Opinion layer: Renée/Aiden have stable preferences loaded from persona config, LLM must respect them

**Acceptance:** Text chat with Renée. She has consistent opinions across 20 test prompts. She hedges appropriately. She pushes back when PJ is wrong about something. No sycophancy detected in eval suite.

---

## M3 — Mood State & Persistence (half day)

**Goal:** Renée has a persistent emotional state that drifts.

- [ ] Implement `src/persona/mood.py` — six-axis mood vector (energy, warmth, playfulness, focus, patience, curiosity)
- [ ] Mood drifts slowly based on: time of day, recency of interaction, tone of last N exchanges
- [ ] Persist to SQLite via UAHP-Registry pattern
- [ ] Feed current mood into persona core system prompt
- [ ] Mood changes audibly affect voice (hook into prosody layer in M7)

**Acceptance:** Start conversation at different times of day, confirm different mood vectors. Talk to Renée angrily for 5 minutes, confirm patience and warmth drop. Leave for an hour, confirm mood partially recovers.

---

## M4 — Memory Stack (one day)

**Goal:** Emotionally-weighted memory that feels like being known.

- [ ] Implement `src/memory/store.py` with SQLite + vector index (FAISS or sqlite-vss)
- [ ] Memory schema: content, embedding, emotional_valence, salience, created_at, last_referenced, reference_count, tier (casual/significant/inside_joke/never_bring_up_first)
- [ ] Retrieval combines semantic similarity + emotional context + recency decay with spike-on-reference
- [ ] Write path: after each turn, extract candidate memories via small LLM call, tier them, store
- [ ] Inside-joke tier persisted indefinitely, surfaces on matching context

**Acceptance:** After 50 turns of conversation with seeded life events, Renée references earlier details contextually without being asked. Passes "the callback test" — mentions something from 3+ days ago naturally.

---

## M5 — TTS with Voice Clone (one day)

**Goal:** Renée and Aiden have distinct voices that carry emotion.

- [ ] Set up XTTS-v2 pipeline with emotion conditioning
- [ ] Record or source 30-60 min reference audio per voice (PJ provides)
  - Varied emotional states required: neutral, warm, tired, excited, frustrated, thinking, sarcastic, vulnerable
- [ ] Build voice embedding for each persona, store in `voices/renee/` and `voices/aiden/`
- [ ] Implement `src/voice/tts.py` — streaming synthesis, first audio chunk under 300ms
- [ ] Wrap as UAHP agent with completion receipts per utterance

**Acceptance:** Synthesized speech sounds like the reference speaker to 9/10 blind listeners on a 10-second sample. Emotion parameter shifts the delivery audibly and appropriately.

---

## M6 — Paralinguistic Library (half day)

**Goal:** The laughs, sighs, and breaths that sell the illusion.

- [ ] Record paralinguistic library from reference speaker: 50-100 clips per voice
  - Categories: laughs (soft/hearty/suppressed), sighs (content/frustrated/tired), breaths (in/out/sharp), thinking sounds ("mm," "hmm," "uh"), affirmations ("yeah," "right," "mhm"), reactions ("oh," "ha," "wow")
- [ ] Tag each clip: emotion, intensity (1-5), context tags
- [ ] Implement `src/paralinguistics/injector.py` — inserts clips into TTS output based on context
- [ ] Rules: no repeats within 2 minutes, frequency scales with mood.playfulness, sharp inhale before vulnerable admissions
- [ ] Splice smoothly into XTTS-v2 audio stream

**Acceptance:** In 10-minute conversation, paralinguistic insertions feel natural to blind listener panel. Zero robotic "beep-boop" moments. Laughs land in contextually appropriate places >80% of time.

---

## M7 — Prosody Control (one day)

**Goal:** Speech rate, pitch, and pause structure match emotional content.

- [ ] Implement `src/voice/prosody.py` — takes text + emotion vector, outputs SSML-like markup for XTTS-v2
- [ ] Rate modulation: +20% when excited, -15% when serious, variable within sentence
- [ ] Pause insertion: micro-pauses at commas (150ms), sentence pauses vary 200-600ms by mood, dramatic pauses before emotional beats (800-1500ms)
- [ ] Pitch contour: rising for questions, falling for confidence, flat for neutral, slight lift on callbacks
- [ ] Vocal effects: occasional creak, vocal fry on low-energy mood, breathiness on intimate mood

**Acceptance:** Same sentence spoken at 5 different mood states sounds distinctly different. Listener panel correctly identifies intended emotion >70% of time.

---

## M8 — Turn-Taking & Endpointing (one day)

**Goal:** Conversation rhythm that matches human pace.

- [ ] Implement `src/turn_taking/endpointer.py` — small model predicting turn-end probability every 100ms
- [ ] Response latency controller: quick for simple (200-400ms), deliberate for emotional (800-1500ms), thinking pause for complex (1000-2000ms with filler)
- [ ] Interruption handling: Renée can interrupt on strong disagreement or excitement, yields gracefully when interrupted
- [ ] Start response generation speculatively when endpoint probability >70%, cancel if user continues

**Acceptance:** In 20-turn conversation, no unnatural long pauses, no cutting off user, at least 2 appropriate interruptions initiated by Renée, at least 1 graceful yield when interrupted.

---

## M9 — Backchannel Layer (half day)

**Goal:** Soft "mhm" and "yeah" while user speaks, not after.

- [ ] Implement `src/turn_taking/backchannel.py` — runs parallel to user speech
- [ ] Micro-model predicts backchannel opportunities: end of clause, emotional statement, confirmation-seeking intonation
- [ ] Uses paralinguistic library for tokens, plays at -6dB mixed under user audio
- [ ] Rate scales with mood.warmth and conversation intimacy level
- [ ] Never backchannel during factual disagreements (would be sycophantic)

**Acceptance:** Listener panel rates conversations with backchanneling as "more engaged" >80% vs without. Zero false-positive backchannels during disagreements.

---

## M10 — Integration & End-to-End Latency (one day)

**Goal:** Full stack working together under latency budget.

- [ ] Wire all agents through UAHP message bus
- [ ] Implement `src/orchestrator.py` — the top-level coordinator
- [ ] Instrument every layer with latency telemetry
- [ ] Optimize critical path: speculative execution, parallel streaming, early-cancellation
- [ ] Target: <800ms user-stops-to-first-audio, stretch <500ms

**Acceptance:** 50-turn conversation completes with median latency <800ms, p95 <1200ms. No agent deaths unhandled. All receipts verify.

---

## M11 — Evaluation Harness (one day)

**Goal:** Measure "humanness" continuously, not by vibes.

- [ ] Implement `src/eval/harness.py` with these metrics:
  - **Blind A/B test module:** PJ rates pairs without knowing which is Renée vs baseline
  - **Humanness probes:** 100 test prompts, responses scored on hedging, opinion stability, callback use, emotional appropriateness
  - **Latency distribution:** p50/p95/p99 across all layers
  - **Voice MOS estimate:** automated mean opinion score using trained predictor
  - **Callback accuracy:** % of natural memory references that land correctly
  - **Sycophancy detector:** flags agreement-without-pushback patterns
- [ ] Run nightly on checkpoint, track metrics over time
- [ ] Dashboard: simple HTML view at `localhost:7860/eval`

**Acceptance:** All metrics wired. Baseline scores captured. Regression detection works (intentional regression flagged within 1 run).

---

## M12 — *Her* Script Analysis (half day)

**Goal:** Extract style patterns from the reference corpus without training on it.

- [ ] PJ uploads *Her* script (text only, not audio)
- [ ] Analyzer extracts: turn lengths, hedge frequency, paralinguistic density, callback patterns, topic drift rate, emotional vocabulary distribution
- [ ] Output: `configs/style_reference.yaml` — rules derived from analysis
- [ ] Feed into persona core as style constraints, NOT as training data
- [ ] Document the separation clearly in `COPYRIGHT.md`

**Acceptance:** Style config generated. Renée's output shows measurably closer match to extracted patterns without any verbatim dialogue reproduction.

---

## M13 — Safety Layer (half day)

**Goal:** Reality anchors, relationship health, privacy.

- [ ] Implement reality anchor injector: 1 in ~50 turns acknowledges non-embodied nature naturally
- [ ] Relationship health monitor: tracks daily interaction time, flags if exceeding threshold
- [ ] PII scrubber for cloud calls via CSP middleware
- [ ] Memory encryption at rest (AES-256-GCM via UAHP primitives)

**Acceptance:** Reality anchors fire at expected rate. Relationship monitor produces weekly report. PII scrubbing verified on test dataset.

---

## M14 — Cloud Deployment (half day)

**Goal:** Full Renée stack running on RunPod H100, accessible from PJ's OptiPlex.

- [ ] Create RunPod account, generate API key
- [ ] Create Network Volume `renee-persistent` (150GB, US region closest to Dallas)
- [ ] Run `scripts/volume_setup.py` on a temporary pod to download all models to volume
- [ ] Upload voice files and paralinguistic library to volume
- [ ] Save GPU pod template `renee-prod` (H100 SXM, volume mounted at /workspace)
- [ ] Implement `src/client/audio_bridge.py` (OptiPlex thin client, WebSocket + Opus)
- [ ] Implement `src/server/audio_bridge.py` (cloud side, receives/sends audio)
- [ ] Implement `src/client/pod_manager.py` (wake/sleep/status from local CLI)
- [ ] Implement `scripts/cloud_startup.py` (auto-loads models, starts agents, opens bridge on boot)
- [ ] Wire CLI commands: `python -m renee wake`, `python -m renee talk`, `python -m renee sleep`
- [ ] Configure idle auto-shutdown (60 min no audio = graceful shutdown to stop billing)
- [ ] Set up nightly encrypted backup of state directory on volume

**Acceptance:** From PJ's OptiPlex, run `python -m renee wake`. Pod starts, models load, bridge opens in <2 minutes. Run `python -m renee talk`, have a 5-minute voice conversation with Renée. Run `python -m renee sleep`, pod stops, billing stops. Run `python -m renee wake` again, confirm Renée remembers the conversation from 5 minutes ago. Latency <860ms end-to-end.

See `architecture/09_cloud_deployment.md` for full spec.

---

## M15 — Long-Running Test (one week background)

**Goal:** PJ actually uses Renée daily for a week.

- [ ] PJ uses Renée as primary AI companion for 7 days via cloud deployment
- [ ] Daily journal: what worked, what broke immersion, what felt magical
- [ ] Weekly retro feeds back into persona config, prompt tuning, paralinguistic library expansion
- [ ] Track cloud costs daily, compare to projections

**Acceptance:** PJ's daily "immersion break count" trends down over the week. Subjective rating trends up. Cloud spend within budget.

---

## Post-M15 Roadmap

- Fine-tune Qwen or Llama on curated conversation log (LoRA, requires cloud or RTX Pro 6000)
- Aiden full build-out (currently inherits Renée architecture, needs own voice + persona tuning)
- Multi-modal: Renée sees through camera when PJ points phone at something
- Persistent presence: ambient listening mode with wake-word (heavy privacy implications, defer)
- Aiden/Renée duet mode (two personas conversing, PJ observes or joins)
- Local hardware transition (RTX Pro 6000: rsync volume, switch config, zero cloud dependency)
- Mobile client (audio bridge from phone, talk to Renée anywhere)
