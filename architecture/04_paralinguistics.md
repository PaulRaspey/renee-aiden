# Paralinguistics Stack

## Purpose
The non-word sounds that separate "AI reading text" from "person talking." The single highest-leverage layer for the uncanny-to-human transition.

## The Theory

Close your eyes and listen to any natural conversation between two people who know each other. Roughly 40 percent of the humanness is in the voice itself; the other 60 percent is paralinguistics. Laughs, sighs, sharp inhales, "mm," the micro-reactions that happen between and underneath words. Most TTS systems have zero. This stack will carry hundreds.

## Library Structure

```
paralinguistics/
  renee/
    laughs/
      soft_01.wav           # quiet chuckle, amused
      soft_02.wav
      hearty_01.wav         # full laugh, surprised
      suppressed_01.wav     # trying not to laugh
      sad_01.wav            # rueful laugh
      nervous_01.wav
      ...
    sighs/
      content_01.wav        # satisfied exhale
      frustrated_01.wav
      tired_01.wav
      thinking_01.wav
      ...
    breaths/
      sharp_in_01.wav       # before vulnerable admission
      slow_out_01.wav       # releasing tension
      thinking_in_01.wav
      ...
    thinking/
      mm_01.wav
      hmm_01.wav
      uh_01.wav
      oh_01.wav
    affirmations/
      yeah_01.wav
      right_01.wav
      mhm_01.wav
      totally_01.wav
    reactions/
      oh_surprise_01.wav
      ha_amusement_01.wav
      wow_01.wav
      ugh_01.wav
    fillers/
      you_know_01.wav
      i_mean_01.wav
      like_01.wav            # use sparingly
```

Metadata per clip (`metadata.yaml`):
```yaml
filename: soft_01.wav
category: laugh
subcategory: soft
emotion: amused
intensity: 0.3               # 0.0 to 1.0
duration_ms: 420
energy_level: 0.4            # fits low-to-medium mood
tags: [casual, warm, agreement]
appropriate_contexts: [friendly_banter, shared_joke, mild_amusement]
inappropriate_contexts: [serious_discussion, disagreement, sad_moment]
```

## Recording Guidelines for PJ

When you record your reference speaker for voice cloning, also capture the paralinguistic library. Separate session, lower pressure. Prompts like:

- "Laugh like you just heard a terrible pun" (soft)
- "Laugh like you genuinely can't believe what you just heard" (hearty)
- "Sigh like you're settling in after a long day" (content)
- "Sigh like you just realized something you didn't want to" (frustrated)
- "Take a breath like you're about to say something hard" (sharp in)
- "Make the sound you make when you're thinking about a problem" (mm/hmm)

Aim for 80-100 clips per voice, multiple takes per emotion.

## Injection Engine

```python
class ParalinguisticInjector:
    def should_inject(self, text: str, mood: MoodState, context: TurnContext) -> list[Injection]:
        """Returns list of (position, clip_selection) injections for this utterance."""
        injections = []
        
        # Rule: vulnerable admission gets preceding sharp inhale
        if context.is_vulnerable_admission():
            injections.append(Injection(
                position=0,
                category="breath",
                subcategory="sharp_in",
                intensity=0.3
            ))
        
        # Rule: shared joke or wit lands with soft laugh
        if context.is_witty_callback() and mood.playfulness > 0.6:
            injections.append(Injection(
                position=END,
                category="laugh",
                subcategory="soft",
                intensity=min(0.5, mood.playfulness)
            ))
        
        # Rule: thinking pause before complex answer
        if context.turn_complexity > 0.7:
            injections.append(Injection(
                position=0,
                category="thinking",
                subcategory="mm",
                intensity=0.3
            ))
        
        # Rule: frustrated sigh on repeated confusion
        if context.user_confused_repeatedly() and mood.patience < 0.4:
            injections.append(Injection(
                position=0,
                category="sigh",
                subcategory="frustrated",
                intensity=0.4
            ))
        
        # Hard constraints
        injections = self._deduplicate(injections)         # no two same category in one utterance
        injections = self._frequency_cap(injections)       # max 2 per utterance
        injections = self._recency_filter(injections)      # no repeat clip within 2 min
        injections = self._mood_filter(injections, mood)   # filter inappropriate to mood
        
        return injections
```

## Selection Within Category

Within a category, clip selection varies:
- Prefer clips not used in last 10 turns
- Match intensity to target
- Match energy_level to current mood.energy
- Randomize within valid candidates (avoid deterministic patterns)

## Splicing Into TTS Output

Two approaches:

### Approach A: Pre-synthesis injection (preferred for XTTS-v2)
Paralinguistic tokens are passed to XTTS-v2 with emotion conditioning, synthesized inline. Modern XTTS-v2 handles some of these natively.

### Approach B: Post-synthesis splice
Synthesize text via TTS, then splice in pre-recorded paralinguistic clips at marked positions. Cross-fade 50ms on each side. Pitch-match the clip to the surrounding synthesized audio via formant shift if needed.

We use both. Approach A for common tokens XTTS-v2 handles well (short "mm," simple breaths). Approach B for distinctive clips (specific laughs, dramatic sighs) where reference fidelity matters.

## The Hard Rule

**No paralinguistics during disagreement or hard truths.**

If Renée is pushing back, correcting PJ, or delivering an unwelcome observation, she does it clean. No laughs to soften, no sighs to excuse. Real people get more serious when the stakes rise. Paralinguistic injection during these moments reads as nervous or placating, which is the opposite of what we want.

## Density Tuning

Target frequency (from the original reference script analysis in `scripts/renee_reference_script.md`):
- Casual conversation: 1 paralinguistic per 2-3 turns
- Playful exchange: 1 per turn
- Serious discussion: 1 per 5+ turns (mostly breaths and thinking sounds)
- Vulnerable moment: 1 per turn, mostly breaths and soft reactions

Too many reads as over-acted. Too few reads as cold. This is the tuning that makes or breaks the illusion.
