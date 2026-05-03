# Session Tonight — Phone → Tailscale → RunPod runbook

Goal: 3-5 conversations of ~30 min each via phone, with full UAHP stack
(persona, prosody, paralinguistics, capture, Beacon liveness).

## Path picker

There are two phone paths. Pick one per session.

| Path | UI | Backend | UAHP? | Session capture? |
|------|----|---------|-------|------------------|
| **A. Existing renee-aiden proxy** | Bare-bones PWA shell at `https://<tailscale>:8766/` | renee-aiden audio_bridge.py on RunPod | yes — full | partial (Part 2 wired but pod-side `register_audio_tap` still deferred) |
| **B. New Replit PWA** | Polished React/Tailwind PWA on Replit deploy | Replit Express bridge → Groq Whisper + LLaMA + ElevenLabs (fallback path) | no — bypasses Renée orchestrator | no |

**Tonight: use Path A** for the documented sessions. Path B is a UX preview
without the full stack — webm/opus → PCM transcoding gap is unresolved.

## Pre-flight (one-time)

1. Tailscale up on phone + OptiPlex. `tailscale ip -4` on OptiPlex should
   match the IP you'll connect to from the phone.
2. RunPod credentials in `.env` (`RUNPOD_API_KEY`, `GROQ_API_KEY`,
   `ELEVENLABS_API_KEY`, `RENEE_VOICE_ID=h8pr4vZSN32hZy70aZCN`).
3. (Optional) Beacon URL in `.env`: `BEACON_URL=https://<your-beacon-deploy>`
   so Renée's heartbeat lands somewhere. Leave unset to skip silently.
4. (Optional) Deploy Beacon (the Replit project) and Memory Bridge to
   keep liveness + handoff context durable across sessions.

## Pod provisioning

The pod ID in `configs/deployment.yaml` (`uopulnt3lmphso`) is two weeks
old; verify it's still alive:

```powershell
.venv\Scripts\python.exe -m renee status
```

If `status != RUNNING` or `public_ip` is empty, recreate:

- A100 SXM, US-KS-2 or US-TX
- **Volume `physical_magenta_nightingale` attached at `/workspace`** (critical — without this every session loses memory)
- TCP ports: 8765 (audio bridge), 22 (SSH for `volume_setup.py`)
- `PUBLIC_KEY` env var = your ed25519 public key
- `.env` on pod: `GROQ_API_KEY`, `ELEVENLABS_API_KEY`, `RENEE_VOICE_ID`

After recreation, populate the volume once:
```powershell
.venv\Scripts\python.exe scripts\volume_setup.py
```

Then update `configs/deployment.yaml` `cloud.pod_id` to the new ID.

## Per-session startup

```powershell
# 1. Bring the pod up + wait for the bridge to listen
.venv\Scripts\python.exe -m renee wake

# 2. (Optional) verify
.venv\Scripts\python.exe -m renee status

# 3. Start OptiPlex-side mobile proxy with HTTPS + cert + QR
$env:RENEE_SKIP_ENCRYPT_WARN = "1"
scripts\start_renee_mobile.bat --https
```

The proxy prints:
- `connect URL: https://<tailscale-ip>:8766/`
- `cert install URL: https://<tailscale-ip>:8766/cert`
- ASCII QR (and `state\renee_connect_qr.png` if your CMD codepage is non-UTF-8)

## On the phone (first connect only)

1. Scan the QR or visit the connect URL.
2. iOS will warn about the self-signed cert. Tap "Visit Website".
3. Visit `https://<tailscale-ip>:8766/cert` and install the CA.
4. **Settings → General → About → Certificate Trust Settings** → toggle
   the renee cert ON. (This step is the one that cannot be scripted.)
5. Re-open the connect URL. It should load without warnings.
6. "Add to Home Screen" gets you the standalone PWA.
7. Tap the mic, speak; the wake-lock holds the screen on.

## Documenting the conversations

Auto-capture is **partial**. The Part 2 capture pipeline is wired
(session recorder, dashboard Sessions tab, triage, review notes,
publishing), but pod-side audio tap registration in `audio_bridge.py`
per-connection is deferred. So tonight you have two options for
documentation:

### Option 1: desktop-mode session for one capture (recommended for the documented session)

For ONE of the 3-5 sessions, drop the phone path and run on the OptiPlex
directly — that path uses `renee talk` which DOES auto-record:

```powershell
scripts\start_renee_recording.bat
```

This launches the dashboard at `http://127.0.0.1:7860` (Sessions tab),
runs the audio bridge with `RENEE_RECORD=1`, and triggers triage on
Ctrl+C. Wears headphones to avoid feedback.

### Option 2: phone path with manual logging

For the phone-path sessions, the OptiPlex proxy logs every WebSocket
frame and the orchestrator writes conversation logs to
`/workspace/state/logs/conversations/YYYY-MM-DD.log` (per OVERNIGHT_TODO
PRIORITY 2 — verify it actually emits before relying on it). For full
recording, use phone screen recording or a separate audio recorder.

## Beacon liveness (optional but per the goal)

If `BEACON_URL` is set in your pod's `.env`, `cloud_startup.py` now:
1. Calls `BeaconClient.from_env(STATE)` — auto-loads or creates
   credentials at `/workspace/state/beacon_credentials.json`.
2. Registers as `renee_orchestrator` with `BEACON_HEARTBEAT_S` (default 30s).
3. Spawns a heartbeat loop for the life of the bridge.

Heartbeats stop when the audio bridge stops; if the pod dies hard
without sending heartbeats for `interval + grace`, Beacon's reaper
issues a signed Ed25519 death certificate. Verify the death cert via
Beacon's `/v1/certificates/<id>/verify`.

## Memory Bridge (optional, for Claude session handoff)

Independent service. If you've deployed Memory Bridge:
- Set `BRIDGE_TOKEN` in its env (no longer falls back to a default after
  tonight's fix).
- Visit `<your-deploy>/settings` to verify connectivity.
- Use `<your-deploy>/new` to capture this session's context for the next
  Claude handoff.

## Shutdown

```powershell
# Phone path: just close the PWA tab.
# OptiPlex proxy: Ctrl+C in the proxy terminal.
# Pod: leave running (idle watcher handles 60-min auto-shutdown), or:
.venv\Scripts\python.exe -m renee sleep
```

Pod is **billing while RUNNING**. The $1.50/hr A100 cost adds up fast —
verify it's stopped at the end of the night.

## Known gaps (won't block tonight)

1. **PWA → audio_bridge audio format mismatch**. Replit PWA's webm/opus
   capture isn't transcoded by the Express bridge. Use Path A or
   the PWA's Groq+ElevenLabs fallback only.
2. **Pod-side `register_audio_tap` per-connection wiring** (Part 2
   deferred). Sessions through the phone path don't auto-record. See
   "Documenting" above.
3. **Phone-side cert trust** is manual one-time per device.
4. **Pod ID in deployment.yaml is two weeks old** — verify before wake.
