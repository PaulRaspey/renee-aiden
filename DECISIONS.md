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
