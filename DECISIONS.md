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
