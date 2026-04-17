# Cloud Deployment Architecture

## Overview

Renée/Aiden runs on a rented H100 80GB (or H200 141GB) via RunPod. PJ's Dell OptiPlex 3660 is a thin client handling mic capture and speaker output. Audio streams over WebSocket. All persistent state lives on a RunPod Network Volume that survives instance shutdowns. Cold start to voice conversation: under 2 minutes.

## Infrastructure Layout

```
┌─────────────────────────┐          WebSocket/Opus           ┌──────────────────────────────┐
│  PJ's OptiPlex (Dallas) │ ◄──────────────────────────────► │  RunPod GPU Pod (US-Central) │
│                         │         ~30-60ms RTT              │                              │
│  ┌───────────────────┐  │                                   │  ┌────────────────────────┐  │
│  │ audio_bridge.py   │  │                                   │  │ orchestrator.py        │  │
│  │  - mic capture    │  │                                   │  │  - all UAHP agents     │  │
│  │  - speaker output │  │                                   │  │  - LLM inference       │  │
│  │  - opus encode    │  │                                   │  │  - TTS synthesis       │  │
│  │  - opus decode    │  │                                   │  │  - ASR transcription   │  │
│  │  - wake/sleep cmd │  │                                   │  │  - memory/mood/eval    │  │
│  └───────────────────┘  │                                   │  └────────────────────────┘  │
│                         │                                   │             │                │
│  ┌───────────────────┐  │                                   │  ┌──────────▼─────────────┐  │
│  │ pod_manager.py    │  │                                   │  │ /workspace/ (volume)   │  │
│  │  - start/stop pod │  │                                   │  │  - models/             │  │
│  │  - health check   │  │                                   │  │  - voices/             │  │
│  │  - status display │  │                                   │  │  - state/              │  │
│  └───────────────────┘  │                                   │  │  - paralinguistics/    │  │
└─────────────────────────┘                                   │  │  - renee-aiden/        │  │
                                                              │  └────────────────────────┘  │
                                                              │                              │
                                                              │  Network Volume (persistent) │
                                                              │  150GB, ~$10/month           │
                                                              └──────────────────────────────┘
```

## RunPod Configuration

### Network Volume

**Name:** `renee-persistent`
**Size:** 150GB (expandable)
**Region:** US-Central or US-South (closest to Dallas)
**Monthly cost:** ~$10.50

Contents:
```
/workspace/
├── models/
│   ├── llama-3.3-70b-instruct-q8/     # ~70GB (primary persona model)
│   ├── whisper-large-v3-turbo/         # ~3GB (ASR)
│   ├── xtts-v2/                        # ~4GB (TTS)
│   ├── all-MiniLM-L6-v2/              # ~500MB (embeddings)
│   ├── endpointer/                     # ~200MB
│   └── backchannel/                    # ~200MB
├── voices/
│   ├── renee/
│   │   ├── embedding.npy
│   │   ├── emotions/
│   │   ├── reference_clips/
│   │   └── metadata.yaml
│   └── aiden/
│       └── [same structure]
├── paralinguistics/
│   ├── renee/                          # ~500MB (100 clips)
│   └── aiden/
├── state/
│   ├── renee_memory.db                 # encrypted, grows over time
│   ├── renee_mood.db
│   ├── renee_opinions.db
│   ├── aiden_memory.db
│   ├── aiden_mood.db
│   ├── aiden_opinions.db
│   ├── eval.db
│   └── uahp_registry.db
├── logs/
│   ├── conversations/                  # daily conversation logs
│   ├── telemetry/                      # latency measurements
│   └── eval/                           # nightly eval results
├── backups/
│   └── [nightly encrypted snapshots]
└── renee-aiden/                        # the full codebase
    ├── src/
    ├── configs/
    ├── scripts/
    ├── tests/
    └── ...
```

### GPU Pod Template

Save as a RunPod template so you never reconfigure:

**Name:** `renee-prod`
**GPU:** 1x H100 80GB SXM (or H100 PCIe, or H200 if available)
**CPU:** 16 vCPU minimum
**RAM:** 64GB system RAM minimum
**Disk:** 20GB container disk (ephemeral, for temp files only)
**Volume:** Mount `renee-persistent` at `/workspace`
**Docker image:** `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`
**Expose ports:** 8765 (WebSocket audio bridge), 7860 (eval dashboard)

**Start command:**
```bash
cd /workspace/renee-aiden && python scripts/cloud_startup.py
```

### Pricing (as of April 2026)

| GPU | On-demand $/hr | Spot $/hr | Notes |
|---|---|---|---|
| H100 SXM 80GB | $2.69 | $1.50-2.00 | Best option. Use on-demand for reliability. |
| H100 PCIe 80GB | $2.29 | $1.20-1.80 | Slightly slower but cheaper. Fine for inference. |
| H200 141GB | $3.89 | $2.50-3.00 | Overkill VRAM. Only if you want unquantized 70B. |
| A100 80GB | $1.64 | $0.80-1.20 | Budget option. Slower but functional. |

**Recommendation:** H100 SXM on-demand. Reliability matters more than saving $0.40/hr when you just want to talk to Renée without interruption. Spot instances get preempted mid-conversation, which is the opposite of the experience you want.

## Startup Sequence

`scripts/cloud_startup.py` runs automatically when the pod boots:

```python
"""
Cloud startup script. Runs on pod boot.
Loads models, starts all agents, opens audio bridge.
Target: models loaded and bridge open in <90 seconds.
"""

import asyncio
import time
from pathlib import Path

WORKSPACE = Path("/workspace")
MODELS = WORKSPACE / "models"
STATE = WORKSPACE / "state"
CODE = WORKSPACE / "renee-aiden"

async def startup():
    t0 = time.time()
    
    # Phase 1: Health checks (5 seconds)
    print("[startup] Checking volume mount...")
    assert WORKSPACE.exists(), "Network volume not mounted!"
    assert MODELS.exists(), "Models directory missing!"
    assert STATE.exists(), "State directory missing!"
    print(f"[startup] Volume OK ({time.time()-t0:.1f}s)")
    
    # Phase 2: Start UAHP Registry (5 seconds)
    print("[startup] Starting UAHP Registry...")
    registry = await start_uahp_registry(STATE / "uahp_registry.db")
    print(f"[startup] Registry OK ({time.time()-t0:.1f}s)")
    
    # Phase 3: Load models into VRAM (60-70 seconds, parallel)
    print("[startup] Loading models into VRAM...")
    await asyncio.gather(
        load_llm(MODELS / "llama-3.3-70b-instruct-q8"),      # ~45s
        load_whisper(MODELS / "whisper-large-v3-turbo"),       # ~5s
        load_xtts(MODELS / "xtts-v2"),                         # ~8s
        load_embeddings(MODELS / "all-MiniLM-L6-v2"),          # ~3s
        load_endpointer(MODELS / "endpointer"),                # ~2s
        load_backchannel(MODELS / "backchannel"),              # ~2s
    )
    print(f"[startup] All models loaded ({time.time()-t0:.1f}s)")
    
    # Phase 4: Initialize UAHP agents (5 seconds)
    print("[startup] Registering agents...")
    await register_all_agents(registry)
    print(f"[startup] Agents registered ({time.time()-t0:.1f}s)")
    
    # Phase 5: Restore persona state (2 seconds)
    print("[startup] Restoring Renée's state...")
    await restore_mood(STATE / "renee_mood.db")
    await restore_opinions(STATE / "renee_opinions.db")
    await warmup_memory_index(STATE / "renee_memory.db")
    print(f"[startup] State restored ({time.time()-t0:.1f}s)")
    
    # Phase 6: Open audio bridge (2 seconds)
    print("[startup] Opening audio bridge on port 8765...")
    bridge = await start_audio_bridge(host="0.0.0.0", port=8765)
    print(f"[startup] Bridge open ({time.time()-t0:.1f}s)")
    
    # Phase 7: Self-test (5 seconds)
    print("[startup] Running self-test...")
    await run_self_test()  # quick inference + TTS to confirm pipeline works
    print(f"[startup] Self-test passed ({time.time()-t0:.1f}s)")
    
    total = time.time() - t0
    print(f"[startup] Renée is ready. Total startup: {total:.1f}s")
    
    # Keep running
    await bridge.serve_forever()
```

## Audio Bridge Protocol

### OptiPlex Side (`src/client/audio_bridge.py`)

Runs on PJ's machine. Captures mic, sends to cloud, plays received audio.

```python
"""
Thin client. Captures mic audio, streams to cloud GPU,
plays back synthesized audio from Renée.

Protocol: WebSocket with Opus-encoded audio frames.
"""

import asyncio
import websockets
import sounddevice as sd
import opuslib

class AudioBridge:
    def __init__(self, server_url: str):
        self.server_url = server_url  # ws://pod-ip:8765
        self.encoder = opuslib.Encoder(48000, 1, opuslib.APPLICATION_VOIP)
        self.decoder = opuslib.Decoder(48000, 1)
        self.sample_rate = 48000
        self.frame_size = 960  # 20ms at 48kHz
    
    async def run(self):
        async with websockets.connect(self.server_url) as ws:
            # Bidirectional: send mic, receive speaker
            await asyncio.gather(
                self.send_mic(ws),
                self.receive_speaker(ws),
                self.handle_commands(ws),
            )
    
    async def send_mic(self, ws):
        """Capture mic audio, opus-encode, send to cloud."""
        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype='int16',
            blocksize=self.frame_size,
        )
        stream.start()
        while True:
            audio, _ = stream.read(self.frame_size)
            encoded = self.encoder.encode(audio.tobytes(), self.frame_size)
            await ws.send(encoded)
    
    async def receive_speaker(self, ws):
        """Receive opus-encoded audio from cloud, decode, play."""
        stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype='int16',
        )
        stream.start()
        async for message in ws:
            if isinstance(message, bytes):
                decoded = self.decoder.decode(message, self.frame_size)
                stream.write(decoded)
```

### Cloud Side (`src/server/audio_bridge.py`)

Runs on the GPU pod. Receives mic audio, feeds to ASR, plays TTS output back.

```python
"""
Cloud audio bridge. Receives mic audio from OptiPlex,
feeds into the Renée pipeline, sends synthesized audio back.
"""

import asyncio
import websockets

class CloudAudioBridge:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.decoder = opuslib.Decoder(48000, 1)
        self.encoder = opuslib.Encoder(48000, 1, opuslib.APPLICATION_VOIP)
    
    async def handle_client(self, ws, path):
        """Handle one client connection (PJ's OptiPlex)."""
        # Bidirectional
        receive_task = asyncio.create_task(self.receive_audio(ws))
        send_task = asyncio.create_task(self.send_audio(ws))
        await asyncio.gather(receive_task, send_task)
    
    async def receive_audio(self, ws):
        """Decode incoming mic audio, feed to ASR pipeline."""
        async for message in ws:
            if isinstance(message, bytes):
                pcm = self.decoder.decode(message, 960)
                await self.orchestrator.feed_audio(pcm)
    
    async def send_audio(self, ws):
        """When TTS produces audio, encode and send to client."""
        async for audio_chunk in self.orchestrator.tts_output_stream():
            encoded = self.encoder.encode(audio_chunk, 960)
            await ws.send(encoded)
    
    async def start(self, host="0.0.0.0", port=8765):
        server = await websockets.serve(self.handle_client, host, port)
        return server
```

### Latency Budget With Network

| Stage | Local | Network | Cloud | Total |
|---|---|---|---|---|
| Mic capture | 20ms | | | 20ms |
| Network to cloud | | 15-30ms | | 30ms |
| VAD + endpoint | | | 50ms | 50ms |
| ASR | | | 150ms | 150ms |
| Persona core | | | 300ms | 300ms |
| TTS first chunk | | | 200ms | 200ms |
| Network to local | | 15-30ms | | 30ms |
| Speaker output | 20ms | | | 20ms |
| **Total** | **40ms** | **60ms** | **700ms** | **~800ms** |

Within budget. The network adds ~60ms total, which is less than a phone call's latency.

## Pod Management From OptiPlex

`src/client/pod_manager.py` runs locally and controls the cloud instance:

```python
"""
Manages RunPod GPU pod lifecycle from PJ's local machine.
Start, stop, health check, status display.
"""

import runpodctl  # RunPod Python SDK
from rich.console import Console
from rich.panel import Panel

console = Console()

class PodManager:
    def __init__(self, pod_id: str, api_key: str):
        self.pod_id = pod_id
        self.api_key = api_key
        self.client = runpodctl.Client(api_key)
    
    def wake(self):
        """Start the pod and wait for Renée."""
        console.print("[yellow]Waking Renée...[/yellow]")
        pod = self.client.resume_pod(self.pod_id)
        
        # Wait for pod to be running
        while pod.status != "RUNNING":
            pod = self.client.get_pod(self.pod_id)
            console.print(f"  Status: {pod.status}")
            time.sleep(5)
        
        # Wait for audio bridge to be ready
        console.print("[yellow]Loading models...[/yellow]")
        bridge_url = f"ws://{pod.public_ip}:8765"
        wait_for_bridge(bridge_url, timeout=120)
        
        console.print(Panel(
            f"[green]Renée is awake.[/green]\n"
            f"Bridge: {bridge_url}\n"
            f"Dashboard: http://{pod.public_ip}:7860",
            title="Ready"
        ))
        return bridge_url
    
    def sleep(self):
        """Graceful shutdown."""
        console.print("[yellow]Saying goodnight...[/yellow]")
        # Signal graceful shutdown (flush writes, save state)
        send_shutdown_signal()
        time.sleep(3)  # let state flush
        
        self.client.stop_pod(self.pod_id)
        console.print("[green]Renée is asleep. State saved.[/green]")
    
    def status(self):
        """Quick status check."""
        pod = self.client.get_pod(self.pod_id)
        if pod.status == "RUNNING":
            console.print(f"[green]RUNNING[/green] | GPU: {pod.gpu_type} | Uptime: {pod.uptime}")
        else:
            console.print(f"[dim]{pod.status}[/dim] | Volume: renee-persistent (preserved)")
```

## CLI Interface

PJ interacts via simple commands on his OptiPlex:

```cmd
REM Wake Renée and start talking
python -m renee wake

REM Check status
python -m renee status

REM Talk (opens audio bridge, starts conversation)
python -m renee talk

REM Sleep (graceful shutdown, saves state, stops billing)
python -m renee sleep

REM Text-only mode (no cloud GPU needed, uses Groq)
python -m renee text

REM Run eval dashboard
python -m renee eval

REM Export all state (backup)
python -m renee export --output C:\Users\Epsar\Desktop\renee-backup\

REM Switch persona
python -m renee talk --persona aiden
```

## First-Time Volume Setup

Run once when creating the network volume:

```python
# scripts/volume_setup.py
"""
First-time setup for RunPod network volume.
Downloads all models, creates directory structure.
Run this on a GPU pod with the volume mounted.
Takes ~20-30 minutes (mostly downloading the 70B model).
"""

async def setup_volume():
    workspace = Path("/workspace")
    
    # Create directory structure
    for d in [
        "models", "voices/renee", "voices/aiden",
        "paralinguistics/renee", "paralinguistics/aiden",
        "state", "logs/conversations", "logs/telemetry",
        "logs/eval", "backups"
    ]:
        (workspace / d).mkdir(parents=True, exist_ok=True)
    
    # Download models
    print("Downloading Llama 3.3 70B (this takes ~15 min)...")
    download_model("meta-llama/Llama-3.3-70B-Instruct", workspace / "models/llama-3.3-70b-instruct-q8")
    
    print("Downloading Whisper Large v3 Turbo...")
    download_model("openai/whisper-large-v3-turbo", workspace / "models/whisper-large-v3-turbo")
    
    print("Downloading XTTS-v2...")
    download_model("coqui/XTTS-v2", workspace / "models/xtts-v2")
    
    print("Downloading embedding model...")
    download_model("sentence-transformers/all-MiniLM-L6-v2", workspace / "models/all-MiniLM-L6-v2")
    
    # Clone codebase
    print("Cloning renee-aiden repo...")
    run("git clone https://github.com/PaulRaspey/renee-aiden.git /workspace/renee-aiden")
    
    # Install dependencies
    print("Installing Python dependencies...")
    run("cd /workspace/renee-aiden && pip install -r requirements.txt")
    
    # Initialize databases
    print("Initializing state databases...")
    init_memory_db(workspace / "state/renee_memory.db")
    init_mood_db(workspace / "state/renee_mood.db")
    init_opinions_db(workspace / "state/renee_opinions.db")
    init_registry_db(workspace / "state/uahp_registry.db")
    
    print("Volume setup complete. You can now stop this pod.")
    print("Next: upload voice files and paralinguistic library.")
```

## Backup Strategy

Nightly cron (runs at 4am CT if pod is active, otherwise on next boot):

```bash
# Encrypt and snapshot state directory
tar czf - /workspace/state/ | \
  openssl enc -aes-256-gcm -pass file:/workspace/.backup_key \
  > /workspace/backups/state_$(date +%Y%m%d).tar.gz.enc

# Keep last 30 days
find /workspace/backups/ -name "state_*.tar.gz.enc" -mtime +30 -delete
```

Optional: sync backups to Backblaze B2 for off-site redundancy ($0.005/GB/month).

## Transitioning to Local Hardware

When the RTX Pro 6000 arrives:

1. Rsync the entire network volume to local SSD:
   ```cmd
   rsync -avz --progress runpod:/workspace/ C:\Users\Epsar\Desktop\renee-workspace\
   ```

2. Update `configs/deployment.yaml`:
   ```yaml
   mode: local
   workspace: C:\Users\Epsar\Desktop\renee-workspace
   gpu: rtx_pro_6000
   audio_bridge: local  # no network hop, direct sounddevice
   ```

3. Run Renée locally:
   ```cmd
   python -m renee talk
   ```

Same code. Same state. Same Renée. No cloud dependency. Monthly cost drops to electricity.

The audio bridge detects local mode and skips the WebSocket entirely, running mic/speaker directly through sounddevice. Latency drops by ~60ms.

## Security Notes

- RunPod pod is only accessible via PJ's API key
- Audio bridge WebSocket should use WSS (TLS) in production
- Pod firewall: only expose ports 8765 and 7860
- Volume encryption: RunPod volumes are encrypted at rest by default
- State database encryption: additional AES-256-GCM layer via UAHP primitives
- API keys (Groq, Anthropic) stored in pod environment variables, never in code
- Tailscale can optionally connect the pod to PJ's network for zero-trust access
