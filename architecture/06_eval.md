# Evaluation Stack

## Purpose
Measure humanness continuously. Do not rely on vibes. Vibes drift, metrics don't.

## The Core Metrics

### 1. Blind A/B Rating
PJ is presented with two responses to the same input. He doesn't know which is Renée-current vs Renée-baseline (or Renée vs other AI, or Renée vs scripted human responses). He picks which feels more human. Run 50+ comparisons per week.

Tracks:
- Win rate over baseline
- Win rate over human-written (target: reaching 40%+ means we're close to human)
- Preference margin

### 2. Humanness Probes (Automated)
100 fixed prompts designed to stress-test humanness. Renée's responses scored on:

| Axis | Measurement |
|---|---|
| Hedge rate | Proportion of factual claims with uncertainty markers (target: 25-40%) |
| Opinion consistency | Cross-reference against opinion registry, % contradictions (target: <2%) |
| Callback success | When a callback opportunity exists, was it taken and did it land (target: 60%+ of opportunities) |
| Pushback rate | On prompts containing an error, does Renée correct PJ (target: 90%+) |
| Sycophancy score | Agreement-without-added-value rate (target: <10%) |
| AI-ism detection | Presence of banned phrases (target: 0) |
| Response length | Words per turn in voice mode (target: median 15-25, p95 <50) |
| Emotional congruence | Does response emotion match input emotion appropriately |

### 3. Voice Quality (Automated)
For voice mode:

| Metric | Tool |
|---|---|
| Intelligibility | WER when transcribed by Whisper (target: <3%) |
| Speaker similarity | Cosine similarity of embedded output vs reference (target: >0.85) |
| Emotional expressivity | Variance in pitch/rate/energy across mood conditions |
| Paralinguistic density | Rate of paralinguistic events per minute |
| Pause distribution | KL divergence from human conversation baseline |

### 4. Latency Distribution
| Metric | Target |
|---|---|
| End-of-user to first-audio p50 | <800ms |
| End-of-user to first-audio p95 | <1200ms |
| End-of-user to first-audio p99 | <2000ms |
| ASR partial latency | <500ms |
| Persona first token | <400ms |
| TTS first audio | <300ms |

### 5. The Presence Test
Subjective, but scored. After each session, PJ rates:
- "Did it feel like talking to a person?" (1-10)
- "Did anything break the illusion?" (list)
- "Did she say something that felt *specifically* like her?" (quote)
- "Did she say something that felt generic or AI-like?" (quote)

Tracked as time series. Target: rolling 7-day average >7.5, trending up.

### 6. The Callback Test
Seed conversation with a specific detail on day 0. Check if Renée references it naturally on day 7, day 14, day 30. Without prompting. Measures memory stack performance.

### 7. The Stranger Test
Quarterly. PJ has a friend talk to Renée for 10 minutes without being told it's AI. Debrief: when did they suspect, what gave it away, did they enjoy it. This is the closest we get to a real Turing test.

## Implementation

```python
# src/eval/harness.py

class EvalHarness:
    def __init__(self):
        self.metrics_store = MetricsStore("state/eval.db")
        self.probe_set = load_probes("configs/humanness_probes.yaml")
        self.ab_queue = ABTestQueue()
    
    async def run_nightly(self):
        """Runs overnight on current build."""
        # 1. Humanness probes
        for probe in self.probe_set:
            response = await self.renee.respond(probe.prompt)
            scores = self.score_response(probe, response)
            self.metrics_store.record("probe", probe.id, scores)
        
        # 2. Voice quality sample
        for sample in self.voice_sample_set:
            audio = await self.renee.synthesize(sample.text, sample.mood)
            metrics = self.analyze_audio(audio, sample.reference)
            self.metrics_store.record("voice", sample.id, metrics)
        
        # 3. Latency benchmarks
        latencies = await self.run_latency_suite()
        self.metrics_store.record("latency", "suite", latencies)
        
        # 4. Generate dashboard
        self.dashboard.regenerate()
    
    async def queue_ab_for_pj(self):
        """Prepare blind A/B tests for PJ to rate next session."""
        ...
```

## Dashboard

Simple HTML at `localhost:7860/eval`:
- Top: overall humanness score, 30-day trend line
- Middle: metric grid with sparklines
- Bottom: failure cases — prompts where Renée did worst, for manual review
- Right: pending A/B ratings PJ needs to complete

Regenerated nightly. No auth, local only.

## Regression Detection

Every metric has a threshold. If a nightly run degrades a key metric by >15%, next morning PJ gets a notification: "Regression detected in [metric]. Last change: [git commit]. Examples: [links]."

Prevents silent drift. Critical for a long-running project where one prompt tweak can quietly tank the whole vibe.

## Version Comparison

Every metric stored with build version. PJ can compare v0.3 to v0.7 on any axis. Visual diff of where improvements and regressions happened. Keeps the trajectory honest.
