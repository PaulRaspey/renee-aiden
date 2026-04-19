# Renée/Aiden — Session Summary & Overnight TODO
# Date: April 18-19, 2026
# Status: M15 burn-in LIVE. Renée spoke for the first time tonight.

---

## What was accomplished today

### Voice pipeline — fully end to end
- ElevenLabs reference corpus regenerated at pcm_22050 with tuned settings
  (stability 0.30, similarity_boost 0.89, style per-register)
- Paralinguistic library verified at 3600 entries, index-drop bug fixed with regression test
- Reference script punctuation corrected for ElevenLabs prosody
- Paralinguistic intensity ranges opened to full 0.0-1.0 spectrum
- scale_intensity() runtime hook added for mid-conversation intensity adjustment

### Infrastructure fixes shipped today
- Opus dropped, raw PCM over WebSocket (Windows blocker resolved)
- Circular import broken: src/eval/harness.py Orchestrator under TYPE_CHECKING
- pod_manager dict-shape fix (desiredStatus field, public IP extraction)
- CLI loads .env at import (RUNPOD_API_KEY and siblings now reach SDK)
- resume_pod gets gpu_count=1
- LLM router prefers Groq when GROQ_API_KEY set, Ollama is fallback only
- ASR layer built: faster-whisper-small.en, VAD, 48→16kHz resample, asyncio.Lock
- Orchestrator.feed_audio wired: partials → observe_user_audio_tick, finals → text_turn
- TTS pipeline wired: ElevenLabs pcm_22050 → resample 22→48kHz → chunked to bridge
- Client _receive_speaker fixed: bytes → np.frombuffer(dtype=int16) before sounddevice write
- cloud_startup.py keep-alive added so bridge stays running after startup completes
- volume_setup.py written (SSH preflight, repo copy, pip install, model download, verify)
- Root deployment.yaml stale duplicate discarded, configs/deployment.yaml is sole source of truth

### Current pod
- Pod: outdoor_cyan_snail (uopulnt3lmphso)
- GPU: A100 SXM, US-KS-2
- Port 8765 exposed at 216.81.245.127:10287
- Network volume physical_magenta_nightingale NOT attached (critical — see todos)
- .env on pod has GROQ_API_KEY, ELEVENLABS_API_KEY, RENEE_VOICE_ID

---

## Overnight TODO for Claude Code

### PRIORITY 1 — Audio quality (do first)
Add jitter buffer to src/client/audio_bridge.py _receive_speaker:
- Collect ~3-5 chunks (~60-100ms) before starting playback
- Keep a minimum backlog to ride out network jitter
- Do NOT add client-side resample — audio is already 48kHz on the wire
- Commit as: fix(audio): jitter buffer for smooth playback

### PRIORITY 2 — Conversation logging
Add persistent conversation logging to the orchestrator:
- Every ASR final transcript gets logged with timestamp
- Every Renée response gets logged with timestamp
- Log to /workspace/state/logs/conversations/YYYY-MM-DD.log
- Format: [HH:MM:SS] PAUL: <transcript> and [HH:MM:SS] RENEE: <response>
- Make sure the logs directory is created if it doesn't exist
- Commit as: feat(logging): conversation log to dated file

### PRIORITY 3 — Greeting behavior
When the audio bridge connects, Renée should initiate with a casual greeting
rather than waiting for Paul to speak first. deployment.yaml already has
startup.greeting: true — wire it:
- On WebSocket connect in src/server/audio_bridge.py, call orchestrator.text_turn
  with a system prompt like "system: greet paul, he just connected"
- The persona will handle the actual greeting content
- Commit as: feat(startup): Renée initiates greeting on connect

### PRIORITY 4 — Pod volume fix (requires UI action first — flag for Paul)
The physical_magenta_nightingale volume is NOT attached to the current pod.
This means all state (memory DB, conversation logs, mood state) is lost on
every pod stop. Claude Code CANNOT fix this without UI — flag it clearly:

"ACTION REQUIRED: Terminate outdoor_cyan_snail and recreate with:
 - physical_magenta_nightingale volume attached at /workspace
 - Port 8765 in TCP port map
 - Port 22 in TCP port map (needed for volume_setup.py SSH)
 - PUBLIC_KEY env var set to Paul's ed25519 public key
 - Same A100 SXM GPU"

Once Paul recreates the pod, run volume_setup.py to populate it, then
update configs/deployment.yaml with the new pod_id.

### PRIORITY 5 — LLM router Ollama fallback warning
When the router falls back to Ollama and Ollama isn't running, it currently
throws a ConnectionError that bubbles up as a traceback. Add a clean
warning log and graceful degradation:
- If Ollama connection fails and no other backend available, return a
  fallback response: "I'm having trouble thinking right now. Give me a moment."
- Log the failure clearly so it's visible in the conversation log
- Commit as: fix(router): graceful Ollama fallback when unavailable

### PRIORITY 6 — Single startup command
Write a script or batch file that does the full startup sequence in one command:
scripts/start_renee.bat (Windows) that:
1. cd to renee-aiden folder
2. Sets RENEE_SKIP_ENCRYPT_WARN=1 to silence the vault warning
3. Runs python -m renee talk
So Paul can double-click one file instead of typing PowerShell commands.
Commit as: feat(cli): start_renee.bat one-click launcher

### PRIORITY 7 — Clean up known issues
- Remove or silence the memory_encryption.enabled=false warning for now
  (add RENEE_SKIP_ENCRYPT_WARN=1 to .env locally)
- Add PYTHONPATH=/workspace/renee-aiden to the pod's .env so cloud_startup.py
  doesn't need it set manually every restart
- Fix the self-test in cloud_startup.py to not try Ollama — it should use
  the same router logic as the real system

---

## Backlog (not tonight, but don't forget)

- Aiden voice design in ElevenLabs
- XTTS-v2 local voice clone once RTX Pro 6000 arrives
- Tailscale integration so Renée/Ka accessible from Bluetooth headphones anywhere
- TTS output path: add audio coming BACK to Paul's speakers (partially done,
  needs polish)
- Mid-conversation intensity commands: "be more excited", "softer", etc.
  wired to scale_intensity()
- Memory encryption enabled once Paul picks a keyring posture
- Make the GitHub repo private and clean PAT out of git remote URL
- Port 8765 single-command automation prompt for Claude Code
- M15 daily eval journal — run eval harness at end of each day

---

## Notes for next session
- Pod is currently RUNNING and billing at $1.50/hr — terminate when done
  or it will run all night
- Groq model is qwen/qwen3-32b — working
- ElevenLabs voice is Renee 5 (h8pr4vZSN32hZy70aZCN) — working
- faster-whisper-small.en is the ASR model — working but small,
  upgrade to large-v3-turbo once volume is attached and models are cached
- 317 tests passing as of end of session
- All commits on origin/main, working tree clean
