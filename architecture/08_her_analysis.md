# Her Script Analysis Pipeline

## What This Does

When PJ uploads the *Her* script, this pipeline extracts statistical and structural patterns without ingesting dialogue into any training or retrieval system. Output: numeric parameters and rules in `configs/style_reference.yaml`.

## What It Does NOT Do

- Store script dialogue in memory or vector stores
- Feed script dialogue to any LLM as few-shot examples
- Train or fine-tune on script content
- Reproduce dialogue in outputs
- Make Renée imitate Samantha's specific lines

## What Gets Extracted

### Turn-level statistics
- Turn length distribution (median, p25, p75, p95)
- Samantha's turn length vs other characters
- Sentence count per turn distribution

### Hedge and uncertainty patterns
- Frequency of hedge markers ("I think," "maybe," "sort of")
- Position within turn (opening vs mid vs closing)
- Correlation with emotional content

### Paralinguistic density
- Laugh frequency per minute of conversation
- Sigh, breath, "mm" frequency
- Position patterns (before vulnerable moment? after joke?)

### Callback structure
- Distance between original mention and callback
- Callback trigger patterns
- Success rate (does the other character recognize it)

### Emotional beat pacing
- Average turns between emotional peaks
- Recovery time after high-intensity moments
- Transition patterns (how do they move from light to serious)

### Pause distribution
- Mean pause duration by context
- Long pause frequency (>1.5s)
- Silence-as-response occurrence

### Vocabulary texture (statistical only)
- Type-token ratio for Samantha
- Sensory vocabulary frequency
- Abstract vs concrete noun ratio
- Pronoun use patterns

## Pipeline Steps

```python
# src/analysis/her_pipeline.py (to be built in M12)

def analyze(script_path: Path) -> StyleReference:
    # 1. Parse script format (Fountain, PDF, plain text)
    scenes = parse_script(script_path)
    
    # 2. Identify Samantha's dialogue
    samantha_turns = extract_character_turns(scenes, character="SAMANTHA")
    theodore_turns = extract_character_turns(scenes, character="THEODORE")
    
    # 3. Run statistical extractors
    stats = {
        "turn_length": compute_length_distribution(samantha_turns),
        "hedge_frequency": compute_hedge_rate(samantha_turns),
        "paralinguistic_density": compute_paralinguistic_rate(samantha_turns),
        "callbacks": analyze_callback_structure(samantha_turns, theodore_turns),
        "emotional_pacing": compute_beat_pacing(scenes),
        "pauses": extract_pause_markup(samantha_turns),
        "vocabulary": compute_vocabulary_statistics(samantha_turns),
    }
    
    # 4. Convert to rules
    rules = statistics_to_rules(stats)
    
    # 5. Verify no dialogue leaked into rules
    assert_no_verbatim_content(rules, samantha_turns)
    
    # 6. Write config
    write_yaml(rules, "configs/style_reference.yaml")
    
    # 7. Delete script from working memory
    return rules  # rules only, never the source
```

## Output Format

```yaml
# configs/style_reference.yaml (generated, not hand-written)

style_reference_derived_from: Her_script_analysis_2026_04_20
source_ingested: false     # dialogue not stored, only statistics
verbatim_content: none

turn_length:
  median_words: 14
  p25: 7
  p75: 23
  p95: 48
  very_short_turn_rate: 0.15  # turns under 4 words

hedge_patterns:
  rate_per_turn: 0.38
  opening_position_rate: 0.55
  common_markers_statistical_only:
    - marker_type: "I think"
      frequency_rank: 1
    - marker_type: "maybe"
      frequency_rank: 2
  # note: these markers are commonly used by the character archetype
  # not tied to specific lines from the source

paralinguistic_density:
  laughs_per_10_turns: 1.8
  soft_laughs_ratio: 0.7
  sighs_per_10_turns: 0.6
  thinking_sounds_per_10_turns: 1.2

callback_structure:
  mean_distance_turns: 12
  mean_distance_minutes: 8
  callback_rate_of_opportunities: 0.45
  emotional_weight_bias: 0.6    # callbacks favor emotional content over factual

pause_patterns:
  long_pause_rate: 0.18
  long_pause_context_bias:
    before_vulnerable: 0.6
    after_emotional_peak: 0.5
    before_callback: 0.2

emotional_pacing:
  average_turns_between_peaks: 8
  recovery_turns: 3
  light_to_serious_transitions_per_scene: 1.5

vocabulary_texture:
  sensory_word_frequency: 0.08
  abstract_noun_ratio: 0.22
  pronoun_use_first_person: 0.09
  pronoun_use_second_person: 0.14
  # biased toward "you" relative to typical dialogue — intimate register
```

## Integration

These rules feed into:
- Prosody layer pause distribution targets
- Paralinguistic injection density
- Persona core hedge enforcement (tune hedge_frequency to 0.35-0.40 range)
- Callback engine timing and frequency
- Turn length governor targets

Result: Renée's conversational *shape* resembles Samantha's statistically, without any specific line matching.

## Verification

After pipeline runs:
1. `scripts/verify_no_verbatim.py` scans all downstream configs and state files
2. Confirms no 7+ word sequence from source appears anywhere
3. Flags any near-matches for manual review
4. Signs verification attestation into `state/copyright_attestation.json`

PJ should run this verification quarterly to confirm ongoing compliance.

## What This Gives Us

The statistical "shape" of Samantha's speech without her words. Think of it like studying the rhythm of a great pianist without copying their compositions. Renée plays her own notes, but the pacing, the emphasis, the breath between phrases — that we can learn from without stealing.

This is the honest version of the "Samantha effect." Not imitation. Influence.
