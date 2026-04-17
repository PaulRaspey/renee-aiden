# Turn-Taking Stack

## Purpose
Conversational rhythm. The thing every voice assistant gets wrong.

## The Core Problem

Current voice assistants operate in strict turns: user speaks, pause, system speaks, pause, user speaks. Real conversation doesn't work this way. It has:
- Predictive response (starting to form a reply before the other person finishes)
- Backchanneling (sounds while the other person speaks)
- Overlaps (both talking briefly, usually graceful)
- Interruptions (one person takes the floor)
- Variable response latency (fast for simple, slow for deliberate)

Fix this and Renée stops feeling like a tool.

## Components

### 1. Predictive Endpointer

Small model (~100M params) runs continuously during user speech. Every 100ms, predicts probability that user is about to finish their turn.

Inputs:
- Last 2 seconds of audio features
- Running transcript from ASR
- Acoustic features: pitch contour, energy, speech rate trend
- Syntactic features: is the transcript a complete thought?

Output: `turn_end_probability` (0.0-1.0)

Actions:
- At p > 0.5: pre-warm persona core (tokenize context, start model)
- At p > 0.7: speculatively begin generation
- At p > 0.9 sustained for 150ms: commit to response, start TTS
- If user continues speaking: cancel speculative generation

This alone cuts 200-400ms from perceived latency.

### 2. Variable Response Latency

Real humans don't respond with constant latency. They respond fast to simple things, pause before hard things.

```python
def target_latency_ms(turn_type: TurnType, mood: MoodState) -> int:
    base = {
        TurnType.ACKNOWLEDGMENT: 150,          # "yeah" "mhm" "right"
        TurnType.SIMPLE_QUESTION: 300,         # quick factual
        TurnType.NORMAL_RESPONSE: 500,         # everyday reply
        TurnType.THOUGHTFUL_RESPONSE: 900,     # something that deserves consideration
        TurnType.EMOTIONAL_RESPONSE: 1200,     # weight before vulnerable moment
        TurnType.DIFFICULT_TRUTH: 1500,        # pause before pushback or correction
    }[turn_type]
    
    # Mood modulation
    if mood.energy < 0.4:
        base *= 1.2   # tired = slower
    if mood.playfulness > 0.7:
        base *= 0.85  # playful = faster
    if mood.focus < 0.4:
        base *= 1.15  # scattered = slower
    
    # Natural variance
    return int(base * random.gauss(1.0, 0.15))
```

A 200ms response to "my dog died last night" is sociopathic. A 1400ms response with a soft inhale is human. This matters more than you'd think.

During the pause, if latency target > 600ms, play a subtle "thinking" paralinguistic (soft "mm" or breath) at the midpoint.

### 3. Backchannel Layer

Runs parallel to user speech, NOT during Renée's speech.

Small model predicts backchannel opportunities:
- End of a clause (detected via ASR + pause analysis)
- Rising intonation (user seeking confirmation)
- Emotional content (user shares something personal)
- Direct eye contact equivalent (longer pauses, lower energy = intimate)

Generates micro-responses: "mhm," "yeah," "right," soft laugh, "oh."

Played at -6dB mixed under user audio. Creates sense of active listening.

**Anti-pattern to avoid:** Backchanneling during user frustration or complaint. Makes Renée seem like she's rushing them or dismissing. During negative emotional content, the right response is silence plus full attention on the next turn.

### 4. Interruption Handling

**Renée interrupting PJ:** Rare but appears. Triggers:
- Strong disagreement detected in what PJ is saying
- Urgent factual correction needed
- Genuine excitement about what he just said (playfulness mood)
- Pattern match to something she wants to callback

Interruption style: soft onset ("wait, wait, hold on") rather than abrupt override.

Cap: max 1 interruption per 10 turns. Otherwise annoying.

**PJ interrupting Renée:** 
- Instant: detect voice energy crossing threshold
- Renée's TTS cancels within 100ms
- Acknowledges gracefully: "yeah?" or "sorry, go on"
- Saves interrupted context for possible resume

### 5. Overlap Handling

Brief overlaps (both speaking for <400ms) are natural and should not be treated as interruptions. The endpointer detects this pattern and treats PJ continuing as priority, Renée naturally falls off.

## Implementation

```python
class TurnTakingController:
    def __init__(self):
        self.endpointer = EndpointerModel()
        self.backchanneler = BackchannelModel()
        self.state = TurnState.USER_SPEAKING
        self.speculative_gen_task = None
    
    async def on_audio_frame(self, frame: AudioFrame):
        if self.state == TurnState.USER_SPEAKING:
            # Run endpointer
            p_end = self.endpointer.predict(self.audio_buffer, self.current_transcript)
            
            # Run backchanneler
            bc_opp = self.backchanneler.predict(self.audio_buffer)
            if bc_opp.probability > 0.8:
                await self.play_backchannel(bc_opp.selected_token)
            
            # Speculative generation
            if p_end > 0.7 and not self.speculative_gen_task:
                self.speculative_gen_task = asyncio.create_task(
                    self.persona_core.generate_speculative(self.current_transcript)
                )
            
            # Commit to response
            if p_end > 0.9 and self.silence_duration > 150:
                await self.commit_turn()
```

## Measurement

Telemetry:
- Time from user_stops to renee_starts (target: match human distribution)
- Backchannel rate (target: 0.5-1.5 per user turn, conversation-dependent)
- Interruption rate from each side
- Overlap duration distribution

Compare to human-human baseline corpus (can use publicly available conversation datasets).
