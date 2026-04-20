# Reference Script Style Analysis Pipeline

## Scope clarification (2026-04-19)

This document originally described a speculative pipeline that would have ingested third-party scripts. That pipeline was never built, and the project does not analyze, store, or derive style from any copyrighted script, audio performance, or fictional character.

The M12 style extractor that actually ships reads exactly one file: `scripts/renee_reference_script.md`, an original work written by PJ specifically as Renée's voice reference. Everything below describes that shipped pipeline.

## What This Does

Given the original reference script, the extractor produces statistical and structural parameters that are written to `configs/style_reference.yaml`. Those parameters feed the prosody planner and persona prompt assembler as measured targets.

## What It Does NOT Do

- Ingest any third-party script, novel, screenplay, or transcript
- Store source dialogue in memory, vector stores, or any retrieval surface
- Feed source dialogue to any LLM as few-shot or system-prompt content
- Train or fine-tune any model weights on the source
- Reproduce the source dialogue verbatim in outputs
- Make Renée imitate a specific fictional character's specific lines

## What Gets Extracted

### Turn-level statistics

- Turn length distribution (median, p25, p75, p95)
- Renée-speaker turn length vs other-speaker turn length in the reference
- Sentence count per turn distribution

### Hedge and uncertainty patterns

- Frequency of hedge markers ("I think," "maybe," "sort of")
- Position within turn (opening vs mid vs closing)
- Correlation with emotional content

### Paralinguistic density

- Laugh frequency per minute of reference conversation
- Sigh, breath, "mm" frequency
- Position patterns (before vulnerable moment? after joke?)

### Callback structure

- Distance between original mention and callback within the reference
- Callback trigger patterns
- Per-scene callback-anchor token extraction (both speakers indexed, per Decision 47)

### Emotional beat pacing

- Average turns between emotional peaks
- Recovery time after high-intensity moments
- Transition patterns from light to serious

### Pause distribution

- Mean pause duration by context
- Long pause frequency (>1.5s)
- Silence-as-response occurrence

### Vocabulary texture (statistical only, no dialogue stored)

- Type-token ratio
- Sensory vocabulary frequency
- Abstract vs concrete noun ratio
- Pronoun use patterns

## Pipeline Steps (as shipped)

See `src/eval/style_extractor.py`. In outline:

```python
from pathlib import Path
from src.eval.style_extractor import extract

rules = extract(Path("scripts/renee_reference_script.md"))
rules.write_yaml(Path("configs/style_reference.yaml"))
```

The extractor refuses to read any script path outside `scripts/renee_reference_script.md` unless explicitly overridden by a local developer, and that override is never exercised in production.

## Integration

These rules feed into:

- Prosody layer pause distribution targets
- Paralinguistic injection density per-tone
- Persona core hedge enforcement (tune hedge_frequency to the measured range)
- Callback engine timing and frequency
- Turn length governor targets

See Decision 49 in `DECISIONS.md` for the "style flows into two places" note on how the measured values reach both the prompt side and the prosody side.

## Verification

After an extraction run:

1. `scripts/verify_no_verbatim.py` (when added) scans downstream configs and state files
2. Confirms no 7+ word sequence from the source appears anywhere the model could retrieve it
3. Flags any near-matches for manual review
4. Signs a verification attestation into `state/copyright_attestation.json`

PJ can run this verification quarterly as a discipline check even though the source is original work.

## What This Gives Us

The statistical shape of the original reference script, without embedding any of its specific lines into retrieval or training. The extractor measures rhythm, emphasis, and breath distribution from one document and emits numeric targets that the rest of the stack consumes. No copyrighted work is touched by this pipeline.
