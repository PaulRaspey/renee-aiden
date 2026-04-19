# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** Mobile PWA bridge verified end-to-end on matrix (2026-04-18). M0, M2-M14 green plus M14.mobile (PWA + proxy + TLS). M1 ASR + live audio still need CUDA for XTTS-v2.
**Branch:** main
**Repo:** https://github.com/PaulRaspey/renee-aiden (private)
**Last commit:** see git log; latest sweep commit below.
**Next milestone:** UAHP/QAL/Groq fallback/deployment.yaml drift sweep, then M15 long-running test.
**Blockers:** Phone-side manual cert trust is the only step a test cannot script. See "Blocked on Paul" at the bottom if present.

**Test summary:** 392 tests passing with 3 DeprecationWarnings from the websockets shim. 4 pre-existing memory tests still fail on HuggingFace network access only.

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

### Deferred

- Two-tailnet-node live check: Paul opens `https://100.78.253.97:8766/` on his phone, accepts the cert, then installs the CA from `/cert`. I print the URL and the QR; this is the one step that cannot be scripted from the OptiPlex.
- Dead config keys in `configs/deployment.yaml` (no current consumer): `cloud.daily_spend_alert_usd`, `cloud.monthly_budget_usd`, `cloud.pod_template`, `cloud.gpu_type`, `cloud.volume_name`, `cloud.volume_size_gb`, `audio.codec`, `audio.opus_*`, `audio.input_device`, `audio.output_device`, `models.persona_*`, `models.endpointer`, `models.backchannel`, `startup.self_test_timeout_s`, all of `backup.*`. These look aspirational (billing alerts, Opus on the wire, backup cron). Prune or wire up as features land; keeping for now since they document the target shape.

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

## What's next

- [ ] UAHP/QAL/Groq-fallback/deployment.yaml drift sweep (task #19).
- [ ] Install audio deps (`sounddevice`, `webrtcvad`, `opuslib`, `faster-whisper`, `runpod`) for live M0/M1/M14 audio-side runs.
- [ ] First RunPod spin-up: run `scripts/volume_setup.py`, then `python -m renee wake`.
- [ ] M15 long-running test, overnight conversation session with eval dashboard snapshots every hour.
- [ ] Hook the A/B queue into the CLI so PJ can rate pairs without leaving the terminal.

## Blocked on Paul

- Phone-side install of the self-signed CA from `https://<matrix-tailscale>:8766/cert`. No way to script; requires tapping "Install" in iOS Safari then "Trust" in Settings, General, About, Certificate Trust Settings.
- Do you want Gemma-primary routing or the current Groq-primary? The meta-harness said "Primary local model Gemma 4 E4B on T400, fallback Qwen 3 32B via Groq API". `decide_backend` currently returns `"groq"` whenever the Groq key is set. Flipping it to Gemma-primary on the OptiPlex is one `if` change; the cascade fallback I just added handles Gemma going offline. Leaving as Groq-primary until you confirm.
- UAHP feature gap: `architecture/07_uahp_integration.md` calls for heartbeats, signed death certificates, a supervising agent, replay-detection ledger, and QAL attestation. None of these ship today. Do we build them into M15 or defer until post-launch?

## Known risks / gotchas

- **Windows asyncio refused-connect is slow.** `websockets.connect` to a closed port on Windows takes about 2 seconds before raising `ConnectionRefusedError` (proactor behaviour), not instant. Any test that measures the proxy's give-up path must budget at least 3 seconds per attempted retry.
- **Tailscale must be running on the OptiPlex.** The proxy auto-detects via `tailscale ip -4`; if `tailscaled` is stopped we fall back to `socket.gethostname()` local IPs which are unreachable from the phone.
- **Node 24 `navigator` is read-only.** Any Node-based shim for `client.js` must use `Object.defineProperty(globalThis, 'navigator', {...})`, not `global.navigator = {...}`. Fixed in `tests/test_client_js_lifecycle.py`.
- **Qwen-on-Groq leaks ip_reminder tags.** Fixed in the filter, but if you swap models double-check.
- **Memory encryption off by default.** `MemoryVault` exists but isn't wired into the SQLite memory store yet.
