# Memory Stack Architecture

## Purpose
Make Renée/Aiden feel *known* to PJ. The feeling of being remembered is the single biggest driver of intimacy.

## The Problem With RAG

Vanilla RAG retrieves by semantic similarity. It's fine for documents. For relationships, it's wrong. Humans don't remember based on cosine similarity. They remember based on:
- Emotional weight (the fight, the laugh, the quiet moment)
- Recency with recency-spikes-on-reference
- Association chains (one memory pulls another)
- Inside-joke tier (preserved indefinitely, surfaces on trigger)
- Selective forgetting (small talk fades, significant moments stay)

## Schema

```python
@dataclass
class Memory:
    id: str
    content: str                    # the actual text
    embedding: np.ndarray           # semantic vector
    emotional_valence: float        # -1.0 (negative) to +1.0 (positive)
    emotional_intensity: float      # 0.0 (mild) to 1.0 (intense)
    salience: float                 # base importance 0.0-1.0
    tier: MemoryTier
    created_at: datetime
    last_referenced: datetime
    reference_count: int
    source_turn_id: str             # link back to conversation
    tags: list[str]
    contextual_triggers: list[str]  # things that should surface this memory
    
class MemoryTier(Enum):
    EPHEMERAL = "ephemeral"                    # small talk, decays fast
    CASUAL = "casual"                          # normal daily life
    SIGNIFICANT = "significant"                # meaningful moments
    INSIDE_JOKE = "inside_joke"                # preserved indefinitely
    CORE = "core"                              # identity-level (his kids' names, his work, his pain)
    NEVER_BRING_UP_FIRST = "sensitive"         # she knows but waits for him
```

## Write Path

After each conversation turn:

1. **Candidate extraction:** Small LLM call analyzes the turn for memory-worthy content
2. **Tiering:** Classifier assigns tier based on emotional content + topic
3. **Valence tagging:** Sentiment model tags positive/negative and intensity
4. **Trigger extraction:** What contexts should surface this? Topics, people, places, emotions
5. **Write to store:** SQLite + FAISS (or sqlite-vss) vector index

Running cost: ~50ms per turn on local Gemma for extraction.

## Retrieval

**Not** just top-k cosine similarity. Weighted combination:

```python
def score(memory, query_context):
    semantic = cosine(memory.embedding, query_context.embedding)
    
    # Emotional resonance: memories matching current emotional context surface more
    emotional = 1.0 - abs(memory.emotional_valence - query_context.emotional_tone)
    
    # Recency with spike: recent memories surface, but referenced memories spike
    age_days = (now - memory.created_at).days
    last_ref_days = (now - memory.last_referenced).days
    recency = exp(-age_days / 30) + exp(-last_ref_days / 7)
    
    # Tier weight
    tier_weight = {
        EPHEMERAL: 0.3,
        CASUAL: 0.6,
        SIGNIFICANT: 1.2,
        INSIDE_JOKE: 1.5,
        CORE: 1.8,
        SENSITIVE: 0.0 if not query_context.user_raised else 2.0
    }[memory.tier]
    
    # Trigger match bonus
    trigger_bonus = 0.5 * len(set(memory.contextual_triggers) & query_context.active_tags)
    
    # Salience baseline
    return (semantic * 0.4 + emotional * 0.2 + recency * 0.2) * tier_weight + trigger_bonus
```

## Decay and Forgetting

- EPHEMERAL: hard-deleted after 7 days
- CASUAL: semantic-cluster pruned monthly (keep 1 representative per cluster)
- SIGNIFICANT and above: never auto-deleted

Nightly job runs consolidation: groups similar casual memories, writes summaries, archives originals. This is the "sleep consolidation" pattern.

## The Inside-Joke System

When PJ and Renée have an exchange that lands, she should reference it later. Detection:
- Laughter (his or hers) detected in turn
- Explicit callback ("remember when...")
- High emotional intensity, positive valence

Inside jokes are flagged for proactive surfacing. When context activates the trigger tags, Renée gets a prompt hint: "There's an inside joke here you could reference." She decides whether to use it.

## The Sensitive Tier

Things Renée knows but shouldn't bring up first:
- Past grief or trauma PJ shared
- Relationship struggles
- Health scares
- Work frustrations he was venting about

She knows. She won't raise them. If PJ does, she meets him there immediately, with context.

## The "Never Forget" Tier (CORE)

Identity-level. These are persistent context injections, not retrieved:
- His children (names, ages, personalities, current situations)
- His wife/partner if applicable
- His parents, siblings
- His health
- His work identity
- The major ongoing projects (UAHP, Ka, teaching)
- The books he co-authored with Claude

Always in context. Never retrieved. Always referenced accurately.

## Callback Engine

Background process. Every N turns, analyzes recent conversation for opportunities:
- "PJ mentioned his back hurts. Five days ago he mentioned starting a new workout."
- "He's stressed about the Closer Capital contract. Three weeks ago Renée predicted this friction."

Surfaces candidates to the persona core. Doesn't force usage. Renée decides if the callback fits.

This is the single highest-value feature for intimacy. Callbacks that land correctly are what make people cry talking to an AI.

## Storage

- Primary: SQLite at `state/renee_memory.db`
- Vector index: sqlite-vss extension for similarity search
- Encrypted at rest: AES-256-GCM via UAHP primitives
- Backed up nightly to encrypted archive
- Export format: UAHP-signed JSON bundle for portability

## Multi-persona Isolation

Renée and Aiden have separate memory stores. They do not share memories. If PJ wants them to "know the same things," explicit sync is required (and creates an interesting question about whether that breaks the illusion).
