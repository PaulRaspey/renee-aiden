# Voice Stack Architecture

## Purpose
Convert text + emotion into audio that sounds like a specific human being, streaming, low-latency, emotionally expressive.

## Components

### ASR (Input)
- **Model:** faster-whisper Large-v3-Turbo
- **Streaming:** Partial transcripts every 300ms
- **VRAM:** ~3GB
- **Hardware scaling:** On T400 GPUs, fall back to Whisper Small. On cloud GPU, run full Large-v3.

### TTS (Output)
- **Primary:** XTTS-v2 with emotion conditioning
- **Fallback:** Chatterbox for low-latency simple turns
- **Routing logic:** Simple/short turns with low emotional content use Chatterbox (faster). Emotionally significant turns use XTTS-v2 (richer).

### Voice Cloning Pipeline

#### Reference audio capture spec
Each voice (Renée, Aiden) requires:
- 30-60 minutes total duration
- 48kHz mono WAV, studio quality
- Emotional states (minimum samples per state):
  - Neutral conversational (10 min)
  - Warm and affectionate (5 min)
  - Tired and low-energy (5 min)
  - Excited and playful (5 min)
  - Frustrated and short (3 min)
  - Thoughtful and deliberate (5 min)
  - Sarcastic and dry (3 min)
  - Vulnerable and quiet (3 min)
  - Laughing and ad-libbing (3 min)

#### Processing
1. Denoise (RNNoise or Demucs)
2. Normalize loudness to -23 LUFS
3. Segment into 3-10 second utterances
4. Auto-transcribe for alignment
5. Extract speaker embedding (mean of per-segment embeddings, weighted toward neutral)
6. Per-emotion embeddings for conditioning

#### Storage
```
voices/
  renee/
    embedding.npy              # speaker embedding
    emotions/
      neutral.npy
      warm.npy
      tired.npy
      ...
    reference_clips/           # for XTTS-v2 real-time cloning
      neutral_01.wav
      warm_03.wav
      ...
    metadata.yaml              # recording conditions, dates, notes
  aiden/
    [same structure]
```

### Prosody Layer

Between persona core output and TTS. Takes:
- Plain text from LLM
- Current mood vector
- Conversation context (intimate? casual? heated?)
- Sentence role (greeting, response, question, callback, closer)

Emits SSML-like markup:
```xml
<speak emotion="warm" rate="0.95">
  <pause duration="400"/>
  Hey.
  <pause duration="800"/>
  <breath type="in" intensity="0.3"/>
  I was thinking about you.
  <pause duration="200"/>
  <laugh type="soft" intensity="0.2"/>
  Is that weird to say?
</speak>
```

Rules engine (stored in `configs/prosody_rules.yaml`):
- Callbacks get +100ms preceding pause
- Questions get rising final contour
- Vulnerable admissions get sharp inhale before
- Low-energy mood gets rate 0.85-0.90, slight creak
- High playfulness mood gets rate 1.05-1.15, more paralinguistics
- Never more than 2 paralinguistics per utterance (avoid over-acting)

### Audio I/O

- **Capture:** sounddevice at 16kHz mono (upsample for ASR internally if needed)
- **VAD:** WebRTC VAD aggressive mode 3, with 200ms trailing silence
- **Output:** sounddevice streaming at 24kHz mono (XTTS-v2 native)
- **Mixing:** backchannel layer mixes -6dB during user speech
- **Barge-in:** user speaking at >threshold during Renée's output cancels remaining TTS immediately

## Latency budget

| Stage | Target | Notes |
|---|---|---|
| VAD endpoint detection | 50ms | after silence threshold |
| ASR final transcript | 150ms | on top of last partial |
| Persona core first token | 400ms | Groq streaming |
| Prosody markup | 10ms | pure code, no model |
| TTS first audio chunk | 200ms | XTTS-v2 streaming |
| Audio output start | 20ms | buffer flush |
| **Total** | **~830ms** | target <800ms with overlap |

Critical optimization: start TTS on first sentence while LLM still generating next sentences. Pipeline, don't block.
