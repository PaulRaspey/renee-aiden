# Copyright Handling

## The Rule

Renée and Aiden are NOT trained on copyrighted voice performances or dialogue. No weights ingest *Her*, *Ex Machina*, *Interstellar*, or any other copyrighted audiovisual work.

## What's Allowed

### Script Text Analysis
The only script the style extractor reads is `scripts/renee_reference_script.md`, an original work written by PJ as Renée's voice reference. No third-party script is ingested. What gets extracted from the original reference:
- Turn length distributions
- Hedge frequency patterns
- Paralinguistic density
- Callback structure analysis
- Emotional beat pacing
- Pause and silence patterns

Output: YAML rules and numeric parameters in `configs/style_reference.yaml`. Not dialogue verbatim. Not used as training data. Never fine-tuning input.

This is the same as studying the rhythm of an original work PJ authored himself; no copyrighted text enters the pipeline.

### Voice Reference Material
Reference voice for Renée and Aiden comes from:
- ElevenLabs Voice Design (generated original voices, not clones of real people)
- ElevenLabs Voice Clone of PJ, a consenting friend, or a paid voice actor
- PJ's own direct recordings with consenting speakers
- NOT clones of celebrity voices, characters, or performances

Explicit banned sources:
- Scarlett Johansson voice (for the obvious reason)
- Any movie/TV character voice
- Any voice not properly licensed or consented

**Critical separation:** The original reference script PJ wrote informs *patterns* (rhythm, pacing, paralinguistic density) via statistical extraction only. No copyrighted performance informs the *sound*. The voice for Renée is created fresh in ElevenLabs, then cloned locally into XTTS-v2 for runtime. No copyrighted audio is touched in either step.

### The ElevenLabs → XTTS-v2 Pipeline
1. Design or clone a voice in ElevenLabs (original voice, not a celebrity match)
2. Generate 30-60 minutes of reference audio across emotional registers
3. Download as high-quality WAV
4. Use as XTTS-v2 reference speaker for local cloning
5. Runtime synthesis happens locally on your hardware
6. ElevenLabs is not in the runtime loop — only in voice creation

This gives us: original voice ownership, local runtime, no celebrity exposure, no ongoing API dependency.

### Style Influence
Writing style can be *inspired by* without copying:
- Reading great dialogue to understand rhythm → fine
- Copying Sorkin dialogue verbatim into training → not fine
- Analyzing Fleabag for paralinguistic density → fine
- Fine-tuning on Fleabag scripts → not fine

Rule: if the output could be compared line-by-line to the source and show substantial copying, we've crossed the line.

## Why This Matters

The OpenAI "Sky" voice situation set the precedent. A major voice product was pulled after a public actor dispute over whether a voice had been modeled on them without consent. Any AI voice project that derives its sound from a copyrighted performance inherits that exposure, full stop.

Instead: Renée sounds like Renée. A distinctive voice we create from consenting source. The qualities that make a companion voice compelling (warmth, specificity, imperfection) are produced by the design of the stack, not by sampling anyone else's performance.

## Enforcement in Code

`scripts/check_copyright.py` runs before any training step:
- Verifies reference audio sources are consented
- Scans training datasets for suspected copyrighted content
- Blocks training if issues detected
- Logs all training data provenance

Every voice clone has an attestation file:
```yaml
voice: renee
reference_source: PJ_recording_2026_04_20
speaker_consent: consent_document_2026_04_20.pdf
celebrity_voice_match_check: passed
commercial_use_allowed: true
```

## The Reference Script Specifically

The only script the analysis pipeline reads is `scripts/renee_reference_script.md`, an original work written by PJ. The pipeline:
1. Extracts statistical patterns only
2. Outputs numeric parameters to config
3. Does NOT store the script text in any training pipeline
4. Does NOT embed script dialogue as retrievable content
5. Produces `configs/style_reference.yaml` with derived rules

The original script serves as a teacher, not a textbook Renée memorizes. No third-party script is ingested here or elsewhere.

## Fair Use Position

We don't make fair use claims. We avoid the question by not using copyrighted material in training at all. Reference and inspiration are human activities. Training is machine ingestion. The distinction matters legally and ethically.
