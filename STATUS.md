# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** M15 pre-burn-in guardrails wired (2026-04-19); UAHP gap closure Part 1 landed 2026-04-20; session capture pipeline (Part 2) landed 2026-04-20 on feat/session-capture. Phases 1-5 of the burn-in preamble all green. src/capture/ now owns session recording with QAL genesis, review dep detection, triage (WhisperX + Parselmouth + pyannote + latency + fatigue + safety extraction), the dashboard Sessions tab, review notes with #tag highlights, selective GitHub publishing with attestation manifests, and one-command startup. Session 1 will mint the live QAL genesis + global_chain_root.json on first record.
**Branch:** feat/session-capture (seven commits on top of main after Part 1 merge; merge manually).
**Repo:** https://github.com/PaulRaspey/renee-aiden (private)
**Last commit:** see git log; feat/session-capture carries 7 commits - session recorder, review deps installer, triage pipeline, dashboard Sessions tab, review notes, GitHub publishing, one-command startup.
**Next milestone:** M15 daily burn-in begins after Part 2 merge. First run is a 2-hour window, neutral-to-good baseline only, with the dashboard Health + Sessions tabs watched for drift and triage-flag surprises.
**Blockers:** Two deferred items in state/m15_readiness.md require the live pod (cold wake, real-bridge latency). Phone-side manual cert trust from prior sessions still outstanding.

**Test summary:** 657 tests passing. Part 2 added 135 tests across seven new capture test files (session recorder 22, install review deps 21, triage 23, dashboard sessions 28, review notes 14, publish 16, start recording 11). Part 1 added 51 UAHP tests, Phase 1-4 added 79 tests earlier. 4 pre-existing memory tests still fail on HuggingFace network access only.

**M15 readiness:** `state/m15_readiness.md` - 13 PASS, 0 FAIL, 2 DEFERRED (wake-from-cold, live p50/p95 latency).

## How to resume

1. `cd C:\Users\Epsar\Desktop\renee-aiden`
2. `.venv\Scripts\activate`
3. `python -m pytest tests/ --ignore=tests/acceptance --ignore=tests/test_memory.py`
4. `python -m renee` to see CLI surface.
5. Mobile: `scripts\start_renee_mobile.bat --https` then open the QR on the phone.

---

## Verified steps (this session: 2026-04-18)

### Step 1: proxy_server static + WebSocket + cert route
Verified 2026-04-18 on matrix.
Exercised: bound a real websockets server, curled `/`, `/manifest.json`, `/sw.js`, `/cert`, `/missing`. Asserted exact Content-Types: `text/html; charset=utf-8`, `application/manifest+json`, `application/javascript` with `Service-Worker-Allowed: /`, `application/x-x509-ca-cert` with `Content-Disposition: attachment; filename="renee-proxy.crt"`, and 404 with `text/plain; charset=utf-8`.
Incidental fixes:
- `src/client/proxy_server.py`: manifest served via `mimetypes.guess_type` (gave `application/json`); replaced with an explicit `_STATIC_ROUTES` table that pins each route's MIME and extra headers. Chrome refused the old service worker because the MIME type was not one of the accepted JS MIMEs.
- `src/client/proxy_server.py`: `/sw.js` now emits `Service-Worker-Allowed: /` so the worker can register for the whole origin rather than just `/static/`.
- `src/client/proxy_server.py`: added `/cert` route that streams the PEM file with `application/x-x509-ca-cert` and a filename so iOS Safari offers to install it. Falls back to 404 when the proxy is run without `--https`.
- `src/client/proxy_server.py`: added `/client.js` route so the PWA shell can reference the JS by path (needed for the resampler tests).

### Step 2: proxy_server reconnect + give-up + 50x stress
Verified 2026-04-18 on matrix.
Exercised: real websockets bridges on two distinct ports with a resolver callable; dropped bridge A mid-stream and asserted the phone WebSocket stayed open while the proxy reconnected to bridge B within 5 seconds. 50 concurrent connect/disconnect cycles left `proxy.active_client_count == 0` and `bridge.active_bridge_conns == set()`. With no bridge reachable, the proxy closes the phone WebSocket with `code=1011` and reason `"bridge unavailable"`.
Incidental fixes:
- `src/client/proxy_server.py`: the bridge URL was resolved once at the top of `_serve`, so when the resolver would have returned a new host (RunPod IP changed or the test swapped bridges), the proxy kept hammering the stale address. Moved resolution inside the retry loop.
- `src/client/proxy_server.py`: added `_clients` dict keyed by connection id + `active_client_count` property so stress tests can assert there are no leaked handler tasks.
- `src/client/proxy_server.py`: switched to exponential backoff clamped to 30 seconds per attempt (was a flat delay) so the give-up path is predictable.
- `tests/test_proxy_server.py`: docstring on the 1011 test notes that Windows `asyncio` takes ~2s to fail a refused TCP connect; `max_reconnects=0` keeps the test under 3 seconds.

### Step 3: Tailscale IP auto-detect
Verified 2026-04-18 on matrix. `tailscale ip -4` returned `100.78.253.97` and `ps.tailscale_ip()` matched exactly.
Exercised: live parity test that runs the CLI and compares its output to the in-process detector.
Incidental fixes: none.
Deferred: the two-tailnet-node live curl (needs Paul's phone). The CLI prints the URL and the QR; run `scripts\start_renee_mobile.bat --https` and scan. Marked under "Blocked on Paul" below.

### Step 4: Self-signed certificate SAN + validity + key strength + /cert
Verified 2026-04-18 on matrix.
Exercised: minted certs into `state/certs/`; parsed the PEM with `cryptography.x509`; asserted SAN included the machine hostname, `localhost`, `127.0.0.1`, and any Tailscale IP passed in `extra_hosts`. Asserted `cert.not_valid_after_utc - cert.not_valid_before_utc >= 365 days` (we ship 3650). Asserted key is RSA 2048+ (we ship 2048).
Incidental fixes:
- `src/cli/main.py`: threaded the minted cert path into `run_proxy(cert_path=...)` so the `/cert` endpoint works only when `--https` is on.
- `src/cli/main.py`: on HTTPS startup the QR print line instructs the user to "install the CA from `<base>/cert`" on first use; avoids the iOS Safari WSS-after-HTTPS double-trust puzzle.

### Step 5: QR code CMD code-page detect + PNG fallback
Verified 2026-04-18 on matrix.
Exercised: `render_qr_ascii` is pure 7-bit ASCII (verified via `.encode('ascii')`), `render_qr_png` writes a valid PNG whose pixel bytes match a fresh `qrcode.make(url)` encoding of the same URL (byte-for-byte, proving no scheme/port/trailing-slash mangling). `terminal_supports_ascii_qr` returns False on non-65001 Windows console code pages and True on UTF-8 or non-Windows.
Incidental fixes:
- `src/client/proxy_server.py`: added `terminal_supports_ascii_qr()` using `ctypes.windll.kernel32.GetConsoleOutputCP`. On non-UTF-8 CMD the ASCII QR renders as garbage characters, so we skip the in-terminal QR and rely on the PNG fallback saved to `state/renee_connect_qr.png`.
- `src/client/proxy_server.py`: `run_proxy` accepts `qr_png_path` and always generates the PNG regardless of code page; absolute path is printed with the connect URL.
- `requirements.txt`: added `pillow` so `qrcode.make` can write PNGs on fresh installs.

### Step 6: AudioWorklet 48000 resampling + overlay unlock
Verified 2026-04-18 on matrix.
Exercised: Python mirror of the JS resampler at `src/client/audio_resample.py`, tested with a 440 Hz sine at 44100 Hz resampled to 48000 Hz. Windowed FFT peak sits within 1 Hz of 440 Hz. Repeated for 22050 Hz (common iOS hardware rate) at 1 kHz. Ran the JS resampler under Node with the same input and verified the output length and zero-crossing frequency estimate match the Python result to two significant figures.
Incidental fixes:
- `src/client/web/client.js`: replaced the old int16-only worklet with a Float32 worklet plus a streaming linear resampler in the main thread. The worklet posts Float32 chunks to the main thread; the main thread interpolates, clamps, and converts to int16 LE before sending over the WebSocket. Carries `tail` (fractional source index) and `lastSample` across chunks so frame boundaries do not tick.
- `src/client/web/client.js`: `unlockAndStart` now aborts and keeps the overlay visible when `audioCtx.state !== "running"` after `resume()`. The overlay is only dismissed once the context is actually running, satisfying the "not dismissable before the context is running" requirement.
- `src/client/web/client.js`: logs `audioContext.sampleRate` at start so Paul can see the rate mismatch in DevTools if playback sounds off.

### Step 7: Backgrounding + wakeLock lifecycle
Verified 2026-04-18 on matrix.
Exercised: ran `client.js` under Node with a hand-rolled DOM shim. Traced `WebSocket` instantiation, `wakeLock.request` and `.release` calls. Scenarios: start session (asserted 1 WebSocket opened + 1 wake-lock acquired), `visibilitychange` with a live WebSocket (asserted still 1 WebSocket, no second connect), `close` event then `visibilitychange` (asserted exactly 2 opens, no reconnect storm), `pagehide` (asserted wake-lock released).
Incidental fixes:
- `src/client/web/client.js`: `connectWS` now guards against double-entry using `ws.readyState` and clears a pending `reconnectTimer` before scheduling a new attempt. Previously a ws.onclose plus a visibilitychange in quick succession could schedule two timers.
- `src/client/web/client.js`: added `stopSession` wired to `pagehide` and `beforeunload` so the wake-lock is released when the page goes away; also clears any pending reconnect timer.
- `src/client/web/client.js`: `unlockAndStart` acquires the wake-lock once at session start rather than firing-and-forgetting at load time.
- `src/client/web/index.html`: moved the inline script to `/client.js` so the proxy can serve it as a named resource and tests can diff against a single canonical copy.

### Step 8: Transcript relay per-client isolation
Verified 2026-04-18 on matrix.
Exercised: two concurrent phone clients connecting to the proxy, each with its own bridge emitting a label-tagged JSON transcript. Phone A received only `hello-A`, phone B received only `hello-B`, no cross-talk. On the orchestrator side, `register_transcript_listener(conn_id, cb)` fans out to all listeners, `unregister()` drops the entry, `transcript_listener_count()` returns zero after both connections end, and re-registering under the same `conn_id` overwrites without duplicate fan-out.
Incidental fixes:
- `src/orchestrator.py`: replaced the single `transcript_emitter` slot with a `{conn_id: cb}` dict plus `register_transcript_listener` / `transcript_listener_count`. The legacy `transcript_emitter` attribute is preserved as a property so existing tests and `cloud_startup.py` keep working; it falls through the fan-out last.
- `src/server/audio_bridge.py`: `handle_client` now registers the emitter via the new API (keyed on `id(ws)`) and calls `unregister()` in `finally`, so the orchestrator holds no reference to a dead socket after disconnect.

### Step 9: UAHP identity + receipt hardening
Verified 2026-04-18 on matrix.
Exercised: sign/verify roundtrip, cross-identity rejection (A's signature does not verify under B), tamper rejection (mutating input_hash breaks the signature), unique receipt_ids + monotone timestamps across back-to-back signings.
Incidental fixes: none.
Deferred: heartbeats, signed death certificates, QAL attestation chain, and a replay ledger. The architecture doc (`architecture/07_uahp_integration.md`) calls for all four, but the code ships only HMAC sign/verify + receipts today. These are Paul-scoped design items, not drift. Added to "Blocked on Paul".

### Step 10: LLM router cascade fallback
Verified 2026-04-18 on matrix.
Exercised: cascade from a failing Ollama backend to a healthy Groq backend in a single generate() call; assert the response comes back under one second in the fake-client harness. Cascade to a canned response only when every configured backend fails. `allow_fallback=False` preserves the raise-on-failure path for callers that want to control retries themselves.
Incidental fixes:
- `src/persona/llm_router.py`: factored `_generate_one` out of `generate`, added `_available_backends` ordering, and a cascade loop. The old inline "canned Ollama fallback" was moved to the all-backends-failed path so a single Ollama connection error no longer locks the user into "I'm having trouble thinking".
- `src/persona/llm_router.py`: Ollama path now raises `ConnectionError` rather than swallowing it, so the cascade can pick up.

### Step 11: deployment.yaml drift sweep
Verified 2026-04-18 on matrix.
Exercised: contract test that walks every cloud.* key load_deployment/cmd_proxy reads and asserts it lives in the shipped yaml with a sane type. Also asserts `cloud_llm.groq.model` and `cloud_llm.anthropic.model` match the router's current hard-coded fallbacks.
Incidental fixes:
- `configs/deployment.yaml`: `cloud_llm.groq.model` was `qwen-3-32b` but `LLMRouter` defaults to `qwen/qwen3-32b`. Changed the yaml to match. Same for `cloud_llm.anthropic.model` (`claude-sonnet-4-6` -> `claude-sonnet-4-5`). Added a drift-watch note explaining that the router reads env vars, not this block, so the values are informational until yaml-driven routing lands.

---

## Verified steps (this session: 2026-04-20)

UAHP gap closure, Part 1. Closes the items Step 9 flagged as deferred (death certs, replay ledger, QAL chain) and adds MemoryVault-UAHP wiring that architecture/07 called for but the code had never implemented. Architectural decision: Option A (inline in a new `src/uahp/` package) because `requirements.txt` keeps the broken PyPI `uahp` wheel commented out and `src/identity/uahp_identity.py` is the vendored local module. One HMAC-SHA256 identity primitive across the stack; every new module imports `AgentIdentity` from the existing file.

### Step 12: UAHP death certificates with task_id and cause (MiniMax patch 1)
Verified 2026-04-20 on matrix.
Exercised: sign/verify roundtrip with all fields populated, tamper rejection on task_id and cause mutations, cross-agent forgery rejection, all 9 DeathCause enum values produce valid certificates, backward-compatibility defaults (task_id="unknown", cause=NATURAL) still sign and verify. 7 tests.
Incidental fixes:
- Dropped the duplicate `AgentIdentity` dataclass the patch shipped, in favour of the canonical one in `src/identity/uahp_identity.py`.
- Left `renee/shutdown.py` untouched: its receipt-based `issue_death_certificate(state_dir, persona)` has a different shape and its tests pin the disk format. The MiniMax cert lives in `src/uahp/death_certs.py`; the two coexist in separate module namespaces.

### Step 13: UAHP task failure certificates (MiniMax patch 2)
Verified 2026-04-20 on matrix.
Exercised: roundtrip with error_message + error_code + metadata, tamper rejection on error_message and task_id, cross-agent forgery rejection, custom error_code preserved through roundtrip, default error_code "UNKNOWN" when unspecified. 6 tests.
Incidental fixes:
- Same AgentIdentity dedup as Step 12.

### Step 14: UAHP dead-agent registry with post-death heartbeat rejection (MiniMax patch 3)
Verified 2026-04-20 on matrix.
Exercised: mark_dead + is_alive, idempotent re-mark with a different cert (still dead, no exception), accept_heartbeat on live agent (no exception), accept_heartbeat on dead agent raises HeartbeatRejectedPostMortem with the agent_id, registry survives process restart (open → close → re-open on same SQLite file → still dead), multi-agent isolation (A dead, B alive, heartbeats from B still accepted). 6 tests.
Incidental fixes:
- `DeathCertificate` import taken from `src.uahp.death_certs` so the registry consumes the same dataclass the patch-1 cert produces.

### Step 15: UAHP replay-detection ledger with retention window (MiniMax patch 4)
Verified 2026-04-20 on matrix.
Exercised: fresh receipt recording, in-window duplicate raises `ReplayDetected`, post-window duplicate silently bumps seen_count (confirms intentional patch-4 semantics: lock window is strict-reject, retention_days is audit retention), 10-thread concurrent recording of distinct receipts all succeed, 10-thread concurrent recording of the same receipt yields exactly one success + nine `ReplayDetected` rejections, stats() reports correct per-agent breakdown, prune() with monkey-patched `time.time` removes entries past retention_days, ledger survives process restart. 8 tests.
Incidental fixes:
- Clarified the module docstring to document the retention_lock_seconds vs retention_days distinction that was implicit in the patch.

### Step 16: MemoryVault-UAHP bridge (MiniMax patch 5)
Verified 2026-04-20 on matrix.
Schema assumption from the patch confirmed against `src/memory/store.py`: the `memories` table has a `tier` column and `recent_turns(n)` returns dicts keyed `user`/`assistant`. No patch adjustments needed on that front.
Exercised: signed memory snapshot roundtrip, tamper rejection on memory_count, cross-agent forgery rejection, snapshot reflects actual memory count after raw-SQL inserts of 3 rows across 2 tiers, latest_memory_hash reflects the most recent turn, memory proof roundtrip + tamper detection, seal_memory_to_death writes a memory_seal block and the re-signed cert verifies. 9 tests.
Incidental fixes:
- Replaced `__import__("sqlite3").connect(...)` with a proper top-of-file `import sqlite3`.
- Added `verify_sealed_death` helper so callers can verify the sealed cert without re-deriving the payload.
- `seal_memory_to_death` no longer mutates its input dict — it operates on a copy and returns the sealed dict, so the original is reusable.
- Guarded `recent_turns` output with `.get("assistant")` / `.get("user")` so an absent key produces "none" rather than a KeyError.
- Test fixture inserts memory rows via raw SQL so the sentence-transformers model never loads; keeps these tests off the HuggingFace network path that `test_memory.py` already owns.

### Step 17: QAL attestation chain with tamper detection
Verified 2026-04-20 on matrix.
Exercised: genesis with all-zero `prev_hash`, append chains of length 2, 5, and 100 all verify clean, `find_tamper` on `action` mutation at idx 3 returns 3, `find_tamper` on `state_hash` mutation at idx 7 returns 7, `find_tamper` on mid-chain swap flags the first broken link, cross-agent forgery rejection (chain signed by Alice fails to verify under Bob's identity), cross-chain `state_hash` collision report surfaces matches without treating them as errors, JSONL serialize + load roundtrip re-verifies, empty chain and single-attestation chain are vacuously valid, `load_chain` on corrupt JSONL raises `ChainLoadError` with the line number (not a raw `JSONDecodeError`), missing-field and missing-file also raise `ChainLoadError` with clear messages, `hash_attestation` detects single-byte mutations. 15 tests.
Incidental fixes: none — this module is net new.

### Deferred

- Two-tailnet-node live check: Paul opens `https://100.78.253.97:8766/` on his phone, accepts the cert, then installs the CA from `/cert`. I print the URL and the QR; this is the one step that cannot be scripted from the OptiPlex.
- Dead config keys in `configs/deployment.yaml` (no current consumer): `cloud.daily_spend_alert_usd`, `cloud.monthly_budget_usd`, `cloud.pod_template`, `cloud.gpu_type`, `cloud.volume_name`, `cloud.volume_size_gb`, `audio.codec`, `audio.opus_*`, `audio.input_device`, `audio.output_device`, `models.persona_*`, `models.endpointer`, `models.backchannel`, `startup.self_test_timeout_s`, all of `backup.*`. These look aspirational (billing alerts, Opus on the wire, backup cron). Prune or wire up as features land; keeping for now since they document the target shape.

---

## Verified steps (this session: 2026-04-20, Part 2 - session capture)

Part 2 closes the session capture loop the Part 1 QAL chain primitive was waiting on. Everything writes into RENEE_SESSIONS_DIR (default `C:\Users\Epsar\renee-sessions\`) on the OptiPlex; nothing runs pod-side.

### Step 18: Session recorder with QAL chain genesis hook
Verified 2026-04-20 on matrix.
Exercised: opt-in start() via flag or RENEE_RECORD=1, 48kHz 16-bit mono WAV capture for mic + renee, bit-for-bit round-trip on canned payloads, KeyboardInterrupt graceful close via context manager, memory snapshot attached + verifiable, first session is genesis (prev_hash all zeros), second session appends (prev_hash links), `global_chain_root.json.bak` created on every write past the first, `verify_chain` green across a 3-session chain concatenation, orchestrator `register_audio_tap(conn_id, mic_cb, renee_cb)` with bit-for-bit parity between consumer and tap, tap-callback failure does not propagate. 22 tests.
Incidental fixes:
- `src/orchestrator.py`: added `_audio_taps` dict + `register_audio_tap` / `audio_tap_count`. feed_audio and tts_output_stream fire their registered callbacks before the real sink (ASR, websocket) so taps observe every chunk even if the consumer cancels mid-stream.
- `src/capture/session_recorder.py`: new module. Chain artifacts are written atomically (tmp + os.replace) and the prior `global_chain_root.json` is copied to `.bak` before each update.

### Step 19: Review dependencies installer
Verified 2026-04-20 on matrix.
Exercised: all five deps (whisperx, praat-parselmouth, pyannote.audio, matplotlib, plotly) detected via `importlib.util.find_spec`; missing-deps listing, estimated-download-MB math that scales with whisper model choice, HF_TOKEN / HUGGING_FACE_HUB_TOKEN detection, ffmpeg PATH check, summary CLI returns 0 when clean + 2 when work remains, warning text includes gyan.dev and chocolatey install hints, .bat wrapper references the python module and ffmpeg. 21 tests (all pure-Python; no pip runs).
Incidental fixes:
- `scripts/install_review_deps.bat`: is a thin wrapper; the Python module owns detection so tests never need to shell out.

### Step 20: Triage pipeline with fatigue and safety integration
Verified 2026-04-20 on matrix.
Exercised: missing whisperx / parselmouth / pyannote imports raise `TriageDepError` with a pointer to the install script (not a raw ImportError trace), pause-flag detection on planted 2.4s gap, high-severity for 2x threshold, pitch-excursion detection with baseline from first 2 min windows, overlap-severity scales with duration (low/medium/high at 0.2s/0.8s/2.0s), mic-silence detection, eval-flag extraction from sycophancy_flag and ai_ism_count, safety-trigger extraction from mock log, fatigue computation on planted decay, latency p50/p95/p99 for known turn timings, end-to-end with multiple planted anomalies, clean session produces empty flag list, ranking is severity-first then timestamp. 23 tests (all runners mocked per clarification 2).
Incidental fixes: none.
Manual validation: `docs/triage_validation.md` is the PJ checklist for first-real-run verification since tests never touch real weights.

### Step 21: Metrics dashboard with cross-session trends and presence score
Verified 2026-04-20 on matrix.
Exercised: Sessions tab nav button, HTML section, and JS all render; list endpoint returns `[]` on empty root and seeded sessions when populated; trends aggregate flag categories + latency + safety rate per session; disk usage reports session count + free bytes + 80% soft warn; per-session detail includes manifest + flags + prosody + latency + overlap + notes; audio streaming endpoint serves mic.wav and renee.wav with `audio/wav` content-type and rejects bad names to block path traversal; POST /presence_score validates 1..5 with Pydantic, rejects out-of-range with 422, returns 409 after `github_published` is true; notes POST/GET round-trip. 28 tests.
Incidental fixes:
- `src/dashboard/config.py`: added optional `sessions_root` (falls back to `default_sessions_root()` when unset).
- `src/dashboard/server.py`: added sessions routes + tab; path traversal is rejected inside `resolve_session_audio`.

### Step 22: Review notes surface with tag-based highlights
Verified 2026-04-20 on matrix.
Exercised: initial notes template includes Overview + one `### [HH:MM:SS]` block per flag with PJ notes stub; ensure_notes_exists is idempotent (first call writes, second call preserves edits); tag regex matches `#harvest` / `#fix` / `#moment` but ignores `#12345` numeric ids and inline suffix like `s1#harvest`; parse_blocks recognizes level-2 and level-3 headings; regenerate_highlights writes both HIGHLIGHTS.md (public only) and HIGHLIGHTS_PRIVATE.md (all tagged blocks), tag order is stable `harvest -> fix -> moment`; dashboard detail creates notes.md on first view and reflects direct-disk edits on next GET; `python -m renee highlights --sessions-root` CLI. 14 tests.
Incidental fixes:
- `src/capture/dashboard_sessions.py`: session_detail now calls `ensure_notes_exists` so the template is always present after first view.

### Step 23: Selective GitHub publishing with attestation manifests
Verified 2026-04-20 on matrix.
Exercised: hard gate on `manifest.public == true` (PublishError on private), hard gate on `presence_score != None` (PublishError when missing), staging copies JSON + markdown but excludes WAV, staging includes chain_manifest.json with prev_hash (continuity proof), opt-in audio ships only Opus via injected encoder (never WAV), redaction rules applied to all .json/.md/.txt/.yaml files in staging, placeholder redaction_rules.json auto-created on first attempt, publish without --confirm only stages (git runner not called), publish with --confirm calls `git add` / `git commit` / `git push` in order and flips manifest.github_published to true, git failure raises PublishError, unpublish removes target repo directory and flips flag back, publish-list filters private + already-published. 16 tests (git + ffmpeg runners mocked).
Incidental fixes:
- `configs/publish.yaml`: new file naming the target renee-sessions-public repo. Pipeline only reads; never overwrites.

### Step 24: One-command startup with recording and dashboard
Verified 2026-04-20 on matrix.
Exercised: pod-unreachable path returns exit code 2 and prints the clear "run `python -m renee wake` first" message without starting the bridge or opening the browser; dashboard already running is detected and re-started is skipped; Ctrl+C during wait still terminates the bridge cleanly and triggers triage on the most recent session directory; empty sessions root produces "no session dir found" and skips triage; latest-session finder picks the newest mtime and skips `_publish_staging`; .bat and .ps1 both set RENEE_RECORD=1 and call the runner. 11 tests (all side effects injected, no real subprocess spawns).
Incidental fixes: none.

### Deferred (Part 2)

- Off-OptiPlex archive: the sessions root is durable on the OptiPlex but not replicated. Sketch a backup target (cloud bucket, or second NAS) before the session count exceeds 30.
- Audio tap wiring on pod-side orchestrator: the `register_audio_tap` contract is in place and tested with bit-for-bit parity, but `cloud_startup.py` does not yet wire the session recorder in. Production recording needs `cloud_startup.py` to instantiate a SessionRecorder against the live orchestrator's identity + memory_store and call `register_audio_tap(conn_id, rec.on_mic_pcm, rec.on_renee_pcm)` plus `register_transcript_listener(conn_id, rec.on_transcript_async)` per connection. Documented here since the .bat wrapper + env var are ready; the wiring edit is one-file and non-invasive.
- MemoryVault encryption, clip library, phone dashboard, off-OptiPlex backup: explicitly out of scope for Part 2. PJ decides after session 1 which to prioritize.

---

## What's done (pre-PWA, left for context)

- [x] Architecture spec + 9 stack deep dives (including cloud deployment)
- [x] Git repo, pushed to GitHub, private
- [x] M0: scaffolding, UAHP identity, first tests
- [x] M2/M3/M4: persona core, mood, memory, text chat REPL
- [x] M5: reference voice corpus, 88 WAVs across 9 emotional registers
- [x] M6: paralinguistic injector + library generator, 3,600 clips, 47.3 min
- [x] M7: prosody layer, rate/pitch/pauses/effects, vulnerable-admission hard rule
- [x] M8: turn-taking, endpointer, latency, interruption
- [x] M9: backchannel layer
- [x] M10: orchestrator, wires persona, paralinguistics, prosody, turn-taking
- [x] M11: eval harness, scorers, A/B, callbacks, style extractor, dashboard
- [x] M12: style extractor + persona/prosody integration
- [x] M13: safety layer (anchors, health monitor, PII scrubber, memory crypto)
- [x] M14: cloud deployment skeleton (RunPod lifecycle, audio bridge shells)
- [x] M14.mobile: PWA proxy with HTTPS, QR, Tailscale detect, per-client transcripts (this session)

## Verified steps (this session: 2026-05-03)

### Step 25: Beacon heartbeat client wired into cloud_startup
Verified 2026-05-03 on matrix.
Exercised: from_env returns None without BEACON_URL; persisted credentials are loaded across "restarts" and discarded when BEACON_URL changes; ensure_registered persists agent_id+api_key to state/beacon_credentials.json and is idempotent on re-call; heartbeat sends `Authorization: Bearer <api_key>`; HTTP 409 (post-mortem) clears credentials so the next start re-registers fresh; transport errors during heartbeat are swallowed (Beacon flakes don't crash the voice loop); run_heartbeat_loop terminates cleanly on stop(). 10 tests, all transport mocked.
Incidental fixes:
- `src/uahp/beacon_client.py`: new module. Stdlib `urllib.request` for HTTP — no new dep.
- `scripts/cloud_startup.py`: post-self-test phase that calls `BeaconClient.from_env(STATE)`, registers, and starts the heartbeat loop. Failure is logged and recorded in `errors` but never crashes startup. `StartupResult.beacon_task` is held so `_serve_forever` can cancel it on shutdown.
Notes:
- `BEACON_URL`, `BEACON_AGENT_NAME` (default `renee_orchestrator`), `BEACON_HEARTBEAT_S` (default 30), `BEACON_GRACE_S` (default 15) are read from env. Leave `BEACON_URL` unset to keep liveness disabled — graceful degradation.

### Step 31: Round 6 — eval prompt, daily cap on phone, topic in log, backup hook
Verified 2026-05-03 on matrix.

Eval prompt at session end (Ctrl+C path): when stdin is a TTY and a session
was captured, launcher asks "How was that session? (1-5, blank to skip)" and
persists via set_presence_score. EOF / out-of-range / non-numeric all skip
silently. --no-score-prompt opts out for headless runs. 5 tests in
test_session_launcher covering blank/out-of-range/valid/non-numeric/EOF.

Daily cap on phone /status: same data the launcher's pre-flight reads,
plumbed through proxy_server's _phone_status_snapshot. status.html grows
a Daily-cap card showing used/total min and remaining with red/warn/ok
coloring at 0 and 30 min thresholds.

Topic header in conversation log: orchestrator._append_conversation_log
writes `# Topic: <text>` on first write of the day when _session_topic
is set; a topic change mid-day produces `# Topic shifted: <text>` so the
context boundary is visible. Uses getattr() fallback so legacy stubs
without _session_topic still work. 2 tests.

Backup hook (src/client/backup.py + scripts/run_backup.py): reads
backup.* from deployment.yaml; one tar.gz per run (or .tar with encrypt:
false); includes chain root + per-session manifests/transcripts/notes/
highlights but excludes WAVs (large + replicable). Retention pruning by
mtime; manifest.jsonl per-archive metadata. enabled:false makes
run_backup a no-op. --force overrides for one-off runs. 8 tests.

### Step 30: Round 5 — dashboard/health surfacing + beacon-setup CLI + phone topic UI
Verified 2026-05-03 on matrix.

Dashboard Health tab: Beacon agent.death panel renders /api/beacon/deaths
as a small table. Cost ledger panel renders /api/cost/history with today/
month-to-date totals and recent-events table; budget state badge flips to
bad when over_budget.

renee migrate-secrets: thin subcommand wrapping scripts/migrate_secrets.py
for discoverability via --help.

renee beacon-setup --url <beacon> [--agent-id ... --webhook-url ...]:
fetches /v1/server/public-key into state/beacon_public_key.b64 (the file
the receiver reads). With both --agent-id and --webhook-url it also
PATCHes /v1/agents/<id> to register the dashboard's webhook endpoint.
Closes the trust loop end-to-end.

PWA /status topic UI: new Topic card with current value + input. The
button POSTs /api/topic which the proxy forwards as a brief WS frame
to the audio bridge's set_topic dispatcher. proxy_server gains
_phone_set_topic() helper with 3 tests.

scripts/volume_setup.py main() now accepts an explicit argv so
pod_manager._default_volume_setup_runner can call main([]) without
inheriting the launcher's CLI flags (real bug — would have crashed
argparse on --auto-provision).

### Step 29: Round 4 — dashboard/logs CLI, cap pre-flight, memory bridge, beacon receiver
Verified 2026-05-03 on matrix.

renee dashboard: opens http://127.0.0.1:7860 in a browser, auto-spawns
the dashboard process if /api/ping doesn't respond. --no-browser to
verify-only. 3 tests.

renee logs [--day YYYY-MM-DD] [-n N] [-f]: tails conversation logs
from state/logs/conversations/. Pure-stdlib polling --follow that
handles mid-write rotation. 4 tests.

Daily cap pre-flight in launcher: reads safety.yaml health_monitor
config + queries HealthMonitor.daily_minutes(); prints "X of Y min used;
Z min remaining today" with [low]/[CAP REACHED] flags. Informational —
doesn't gate. 4 tests.

Volume setup arg fix: scripts/volume_setup.py main() honors an explicit
argv list (was reading sys.argv unconditionally and would have crashed
when called from pod_manager._default_volume_setup_runner with the
launcher's flags). Also adds clear "starting/complete" banners around
the long-running call.

src/client/memory_bridge_client.py: HTTP client for /v1/handoffs.
HandoffPayload + MemoryBridgeClient + build_session_handoff. Wired
into the launcher's session-end shutdown path so a Ctrl+C auto-captures
handoff context for the next Claude session. 9 tests.

src/server/beacon_receiver.py: HMAC-SHA256 verification + JSONL journal
for Beacon's agent.death webhooks. Refuses every webhook when
BEACON_PUBLIC_KEY isn't configured (fail-closed). Dashboard endpoints
/api/beacon/webhook (POST receiver) + /api/beacon/deaths (read-side).
14 unit tests + 3 dashboard route tests.

### Step 28: Launcher v3 — Python API, secrets, ledger, status page, chaos
Verified 2026-05-03 on matrix.

Eight more operator-facing features rounding out the launcher surface from the
followup punch list (#3-#10). Builds on Step 26's #1-#10 batch and Step 27's
UAHP closure.

#3 dashboard SPA cost badge (server.py + inline JS): polls /api/cost every 30s,
color-shifts to warn at $2 and bad at $5. Title shows hourly rate + status.

#4 auto-volume-setup in PodManager.provision(): after create_pod returns the
new SSH port, polls TCP 22 with `_wait_for_ssh` (configurable timeout, defaults
to 120s), then runs scripts/volume_setup.main() via importlib so we don't shell
out for what's already a Python module. Failures recorded in result["volume_setup"].
Launcher gets --with-volume-setup flag.

#5 renee.api Python surface (renee/api.py): typed dataclasses (PodInfo,
WakeResult, TriageResult) and thin functions (pod_status, wake_pod, sleep_pod,
provision_pod, triage_session, latest_session_dir, publish_session, publish_list,
cost_summary). Re-exported from `renee` package — ``from renee import wake_pod``
works without touching `src.*`. 11 tests.

#6 renee.secrets keyring layer (renee/secrets.py): optional `keyring` import
wrapped behind _keyring(); get/set/delete + migrate_env_to_keyring/
populate_env_from_keyring. KNOWN_SECRETS lists every name renee recognizes.
scripts/migrate_secrets.py is the one-time CLI; --check reports where each
secret lives. populate_env_from_keyring runs at launcher startup so existing
os.environ.get callsites work transparently after migration. 12 tests, all
backends mocked.

#7 Path B integration tests (tests/integration/test_path_b_transcode.py):
exercises ffmpeg with the exact args from artifacts/api-server/src/lib/ws-handler.ts.
Verifies 1-second tone yields exactly 96000 PCM bytes; 5-second streamed in
10×100ms chunks preserves length; amplitude is well above noise floor; invalid
input returns nonzero; WAV-wrapped PCM is decodable by ffmpeg (proxy for
AudioContext.decodeAudioData accepting it on the PWA). Skipped when ffmpeg
not on PATH. 5 tests.

#8 Multi-session cost ledger (src/client/cost_ledger.py): SQLite table
`pod_events` with up/down rows; record_up at wake, record_down at session
stop. buckets() aggregates today + month from substring-matched ISO timestamps;
respects monthly_budget_usd from deployment.yaml. /api/cost/history dashboard
endpoint surfaces totals + last 20 events. Ledger auto-creates the DB +
parent dir; concurrent SQLite writes from 4 threads × 10 ops each don't
corrupt (chaos test). 8 tests.

#9 Phone-side status page (#9): /status, /status.html, /status.js added to
proxy_server's _STATIC_ROUTES; /api/status assembles {pod, cost, beacon}
into one JSON; /api/sleep POSTs through to PodManager.sleep(). Phone shell
is dark-mode HTML with native CSS — no framework. Polls every 10s; "Stop the
pod" button fires confirm() then /api/sleep. 6 tests covering the helper
functions and the static-route table.

#10 Chaos tests (tests/integration/test_chaos.py): pod stuck in STARTING,
network flap during heartbeat, Beacon 500 (must NOT clear creds, only 409
does), transcript fan-out isolation when one listener raises, recorder
start() crash, Tailscale CLI nonzero, concurrent ledger writes, keyring
unavailable, ledger DB auto-create. 10 tests.

Test sweep: 764 passed, 5 skipped, no regressions (was 695 going in).

### Step 27: Pod-side audio_tap wiring + topic propagation
Verified 2026-05-03 on matrix.

Closes the Part 2 deferred items from Step 18.

Per-connection SessionRecorder in CloudAudioBridge: when RENEE_RECORD=1 (or
the bridge ctor's `recording_enabled` override is True), `_maybe_start_recorder`
pulls identity + memory_store off `orchestrator.persona_core`, constructs a
SessionRecorder via the injected factory (or the real class), starts it, and
registers (a) an audio tap with mic_cb=on_mic_pcm + renee_cb=on_renee_pcm
keyed `recorder:<id(ws)>`, and (b) a transcript listener keyed
`recorder-tr:<id(ws)>` with cb=on_transcript_async. Both unregister on
disconnect; recorder.stop() runs in `finally`. The recorder factory is
test-injectable so tests don't write WAVs; failures during start are
swallowed and the bridge keeps serving. 6 tests in audio_bridges_smoke.

set_session_topic + topic-aware greeting: orchestrator stores
`_session_topic` (trimmed, capped at 200 chars), greet_on_connect weaves
it into the system prompt as "greet paul, who wants to talk about: {topic}.
Open with one short sentence acknowledging the topic so he knows you've
registered it." JSON dispatch in audio_bridge `_receive_audio` was
previously dropping non-binary frames silently; now `_dispatch_text_message`
parses control frames and routes set_topic (text or topic field alias)
to set_session_topic. Errors swallowed — bridge never crashes on a bad
client frame.

PWA wiring: client.js reads `?topic=` from URLSearchParams on first
WS open and emits `{type: "set_topic", text: ...}` before any audio.
proxy_server reads RENEE_SESSION_TOPIC env and rewrites printed connect
URLs + the QR-encoded primary URL to include `?topic=<urlencoded>`,
so scanning the QR carries the topic onto the phone. Launcher --topic
exports the env var so the proxy picks it up.

5 orchestrator tests cover set_session_topic + topic-aware greeting;
5 audio_bridge tests cover _dispatch_text_message including alias/
unknown-type/invalid-JSON branches. proxy_server, client.js lifecycle
tests still green.

Architectural decisions:
- Topic is per-connection, not per-pod. The PWA owns the topic; the
  bridge is stateless about it across reconnects (the next ?topic= wins).
- JSON control frames over the audio WS instead of a separate channel.
  Simpler, and renee-aiden audio_bridge already had the WS open anyway.

### Step 26: Session launcher v2 — single-button UX additions
Verified 2026-05-03 on matrix.
Exercised: argparse coverage including `--topic`, `--gpu cheap|default|best`, `--auto-provision`, `--yes`, `--with-beacon`, `--with-memory-bridge`, `--no-triage-on-stop`. Tailscale auto-up via TAILSCALE_AUTHKEY env probes for IP, runs headless `tailscale up --authkey=...`, re-probes for IP, all mocked. Pod auto-provision creates a pod and rewrites `cloud.pod_id` in deployment.yaml in-place (preserves comments + inline notes), with TypeError fallback when SDK rejects `network_volume_id`. GPU_TIERS map covers cheap/default/best. Topic banner prints with the requested topic visible. Cost summary picks the right GPU rate from a substring match on the GPU display name and computes cost from elapsed time. Latest-session-dir finder skips `_publish_staging`-style underscored dirs and dirs without manifest.json. Triage trigger spawns `python -m renee triage <session-dir>` in background. /api/cost dashboard endpoint returns `ok:false` cleanly when status() raises and computes `session_usd = uptime/3600 * rate` with substring rate match. 38 tests across launcher + pod_manager + dashboard.
Incidental fixes:
- `src/client/pod_manager.py`: added `provision()` method using runpod SDK's `create_pod`. Falls back to bare-kwargs retry on TypeError (older SDK versions don't accept `network_volume_id` at create time). Added `_persist_pod_id` that surgically rewrites `cloud.pod_id:` in YAML preserving the rest of the file.
- `src/client/web/index.html` + `src/client/web/client.js`: added `#cert-overlay` shown after `CERT_OVERLAY_FAILURE_THRESHOLD` consecutive WS closes that never reached open, when the page is HTTPS. Walks the user through the iOS Settings sequence (`Cert install → VPN & Device Management → Certificate Trust Settings`) that can't be scripted.
- `src/dashboard/server.py`: new `/api/cost` endpoint surfaces pod-up minutes × GPU hourly rate so a stale tab kept open through the night reveals the running spend.
- `scripts/publish_session.bat` + `.ps1`: thin wrappers around `python -m renee publish --confirm <session-id>` for one-button shipping after a good session.

## What's next

- [x] UAHP gap closure Part 2 (session capture pipeline + dashboard Sessions tab + QAL chain genesis on first record) - landed 2026-04-20 on feat/session-capture.
- [x] Beacon heartbeat client + cloud_startup wiring - landed 2026-05-03 on feat/session-capture.
- [x] Session launcher v2 with auto-provision + cost telemetry + cert overlay + publish button - landed 2026-05-03 on feat/session-capture.
- [x] Pod-side `register_audio_tap` per-connection wiring in `audio_bridge.py` (Step 27) - landed 2026-05-03 on feat/session-capture. Closes the Part 2 deferred item.
- [x] Topic propagation end-to-end (launcher --topic → URL ?topic → PWA → bridge → orchestrator.set_session_topic → topic-aware greet) - landed 2026-05-03 on feat/session-capture.
- [x] Launcher v3 (Python API, secrets layer, cost ledger, phone status page, chaos tests) - landed 2026-05-03 on feat/session-capture.
- [ ] Off-OptiPlex archive plan: sessions durable on OptiPlex, not replicated. Sketch backup target before session count exceeds 30.
- [ ] Install review deps on the OptiPlex: `scripts/install_review_deps.bat` then accept the pyannote terms on HF and set HF_TOKEN.
- [ ] UAHP heartbeat emission from each agent (Part 3) so dead-agent registry + replay ledger see live traffic.
- [ ] Install audio deps (`sounddevice`, `webrtcvad`, `opuslib`, `faster-whisper`, `runpod`) for live M0/M1/M14 audio-side runs.
- [ ] First RunPod spin-up: run `scripts/volume_setup.py`, then `python -m renee wake`.
- [ ] M15 long-running test, overnight conversation session with eval dashboard snapshots every hour.
- [ ] Hook the A/B queue into the CLI so PJ can rate pairs without leaving the terminal.

## Blocked on Paul

- Phone-side install of the self-signed CA from `https://<matrix-tailscale>:8766/cert`. No way to script; requires tapping "Install" in iOS Safari then "Trust" in Settings, General, About, Certificate Trust Settings.
- Do you want Gemma-primary routing or the current Groq-primary? The meta-harness said "Primary local model Gemma 4 E4B on T400, fallback Qwen 3 32B via Groq API". `decide_backend` currently returns `"groq"` whenever the Groq key is set. Flipping it to Gemma-primary on the OptiPlex is one `if` change; the cascade fallback I just added handles Gemma going offline. Leaving as Groq-primary until you confirm.
- UAHP feature gap — **partially closed** (2026-04-20). Part 1 shipped death certificates with cause, task failure certificates, dead-agent registry, replay-detection ledger, memory-vault wiring, and the QAL attestation chain primitive (see Steps 12-17). Heartbeat emission, the session capture pipeline that actually mints the QAL genesis, and dashboard surfaces for these are Part 2. Do we run Part 2 before the first M15 burn-in window, or defer to post-launch?

## Known risks / gotchas

- **Windows asyncio refused-connect is slow.** `websockets.connect` to a closed port on Windows takes about 2 seconds before raising `ConnectionRefusedError` (proactor behaviour), not instant. Any test that measures the proxy's give-up path must budget at least 3 seconds per attempted retry.
- **Tailscale must be running on the OptiPlex.** The proxy auto-detects via `tailscale ip -4`; if `tailscaled` is stopped we fall back to `socket.gethostname()` local IPs which are unreachable from the phone.
- **Node 24 `navigator` is read-only.** Any Node-based shim for `client.js` must use `Object.defineProperty(globalThis, 'navigator', {...})`, not `global.navigator = {...}`. Fixed in `tests/test_client_js_lifecycle.py`.
- **Qwen-on-Groq leaks ip_reminder tags.** Fixed in the filter, but if you swap models double-check.
- **Memory encryption off by default.** `MemoryVault` exists but isn't wired into the SQLite memory store yet.
- **QAL chain continuity depends on the `global_chain_root` reference not being deleted.** If lost, the chain breaks and session continuity cannot be cryptographically proven from that point onward. Part 2 closes the loop: `src/capture/session_recorder.py` writes `<RENEE_SESSIONS_DIR>/global_chain_root.json` on every session and also writes `global_chain_root.json.bak` with the prior contents, so a crash mid-rewrite leaves a last-known-good. Both files together + the per-session `attestation_chain.jsonl` are the chain's load-bearing artifacts. The off-OptiPlex archive plan must cover all three, not just the sessions themselves.
- **`configs/publish.yaml` names a target repo that does not exist yet.** Default points at `renee-sessions-public` which PJ has not created. `renee publish --confirm` will fail on the push step until the repo exists and `state/renee-sessions-public/` is a clone of it. Until PJ creates the repo, `renee publish <session>` without `--confirm` works end-to-end (writes staging only) for human review.
