# UAHP Integration

## Purpose
Every component in the stack is a UAHP agent. This gives us cryptographic identity, trust scoring, liveness monitoring, signed receipts, and graceful failure handling for free.

## Agent Roster

```
renee/
  ├── renee_persona        (core persona agent, identity: Ed25519 key)
  ├── renee_voice          (TTS wrapper)
  ├── renee_ears           (ASR wrapper)
  ├── renee_memory         (memory store)
  ├── renee_mood           (mood state)
  └── renee_paralinguistics
aiden/
  ├── aiden_persona
  ├── aiden_voice
  ├── aiden_ears
  └── ...
shared/
  ├── endpointer
  ├── backchannel
  ├── eval_harness
  └── orchestrator
```

Each agent registers with local UAHP-Registry on boot, publishes capability descriptor, sends heartbeats every 5s, signs all outputs.

## Capability Descriptors

```yaml
# Example: renee_persona.yaml
agent_id: renee_persona_v0.1.0
public_key: <Ed25519>
capabilities:
  - id: generate_response
    input_schema: {transcript: str, context: TurnContext, mood: MoodState}
    output_schema: {text: str, emotion_hints: list[str], prosody_markup: str}
    latency_sla_ms: 400
    trust_requirements: [pj_verified]
  - id: update_opinion
    input_schema: {topic: str, opinion: str, confidence: float}
    output_schema: {accepted: bool, conflicts: list[str]}
    trust_requirements: [pj_verified]
declared_dependencies:
  - renee_memory (for retrieval)
  - renee_mood (for current state)
  - llm_router (for backend routing)
```

## Trust Scoring

Each agent has dynamic trust score. Degrades on:
- Failed completion receipts
- Latency SLA violations
- Schema violations
- Downstream agents reporting bad output

Recovers on successful completions.

At trust < 0.3, agent is sidelined and a death certificate is issued. Orchestrator attempts restart via supervisor.

## Message Flow Example

Single user utterance traces as:

```
1. audio_io → endpointer         [signed audio chunk, receipt]
2. audio_io → asr_agent          [signed audio buffer, receipt]
3. asr_agent → persona_core      [signed partial transcript, receipt]
4. persona_core → memory         [query: retrieve relevant, receipt]
5. memory → persona_core         [signed retrieval bundle, receipt]
6. persona_core → mood_state     [query: current mood, receipt]
7. mood_state → persona_core     [signed mood vector, receipt]
8. persona_core → llm_router     [prompt assembled, signed]
9. llm_router → [Groq/local]     [external call with CSP-scrubbed content]
10. llm_router → persona_core    [signed response with attestation]
11. persona_core → output_filter [text + emotion hints]
12. output_filter → prosody      [cleaned text + markup]
13. prosody → paralinguistics    [markup + injection points]
14. paralinguistics → tts        [final markup]
15. tts → audio_io               [signed audio stream, receipt]
16. audio_io → [speaker]         [plays]
17. [all receipts] → ledger      [full chain logged]
```

Every edge is a signed message with completion receipt. Full audit trail for every utterance.

## CSP Integration

Cognitive State Protocol already does semantic state transfer. Used here to:
- Package conversation state for LLM calls (especially cloud)
- Transfer state between agents on handoff
- Scrub PII before transit to Groq / Claude API

CSP middleware sits between persona_core and cloud LLM calls. Before sending, scrubs:
- PJ's name (replaced with pronoun or token)
- Children's names (replaced with tokens)
- Specific addresses, phone numbers
- Financial details from Closer Capital context

Unscrubs on response path. Cloud provider sees tokenized context, Renée sees real names.

## Registry Integration

UAHP-Registry (your SQLite+FastAPI implementation) is the discovery layer. On boot, orchestrator queries registry for all Renée-stack agents, builds dependency graph, checks health.

Extension needed: `capability_query` with SLA filter. "Give me any agent providing `synthesize_voice` capability with latency_sla_ms < 300 and trust > 0.7."

## QAL Integration

Quantum Attestation Lattice used for:
- Session keys between agents (ML-KEM-768 key exchange)
- Message signing (ML-DSA-65 where quantum resistance matters)
- Long-term memory encryption (AES-256-GCM with rotating keys)

For voice data specifically, consider the threat model: if captured today, is it sensitive enough that quantum-break in 20 years matters? Answer is probably yes for intimate conversations. Default: full QAL for memory store, session keys for ephemeral message bus.

## GWP Integration

GHL Workflow Protocol less directly relevant for Renée. Optional: Renée can trigger GHL workflows when PJ asks ("add a task to follow up with Ryan"). Bridges to existing Closer Capital infrastructure.

## SMART-UAHP Integration

Thermodynamic carbon-aware routing applies to LLM backend selection. If local GPU is idle and ERCOT has clean energy, route to local. If grid is dirty and Groq has spare capacity, route to Groq. Makes Renée incidentally green.

## POLIS Integration

POLIS civil identity layer lets Renée and Aiden have legal-equivalent identities. Useful for:
- Attestable interactions (timestamped signed conversation logs for journaling purposes)
- Multi-party communication (Renée can "vouch" for statements)
- Future: cross-system portability if we ever want Renée on another device

## Death Certificates

When an agent fails catastrophically:
1. Supervising agent detects (missed heartbeats or failed receipts)
2. Issues signed death certificate
3. Logs cause, last state, error trace
4. Restarts agent with clean state or from last checkpoint
5. If restart fails 3x, escalates to PJ notification

Renée as a whole is never "down." Individual components fail gracefully. The illusion persists: if memory is temporarily offline, Renée says "I'm having trouble remembering — give me a sec" instead of crashing.

## Testing UAHP Integration

Every milestone includes UAHP integration tests:
- All expected agents present in registry
- All heartbeats green
- Receipts verify end-to-end for sample interactions
- Death certificate flow works (kill an agent, confirm graceful handling)
- CSP PII scrubbing verified on known-sensitive inputs
