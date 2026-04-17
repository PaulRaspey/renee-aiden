# Voice Audition Guide

## For PJ's ElevenLabs Voice Design Sessions

This doc walks you through designing and selecting Renée and Aiden's voices. Budget 2-3 hours per voice. Don't rush this. The voice is half the experience.

---

## Step 1: Generate Candidates

Use ElevenLabs Voice Design (Voice Library → Create → Design).

### Renée
Generate 20-30 candidates using variations of:
> "Late 20s American woman. Lower register, noticeable vocal rasp. Warm with an edge. Voice has texture and grit, not polished. Slightly breathy on quiet delivery. Voice catches and cracks on emotional content."

Vary the phrasing across generations. ElevenLabs responds differently to different descriptions of the same quality. Try:
- "raspy" vs "textured" vs "gravelly warmth"
- "lower register" vs "alto range" vs "deeper feminine voice"
- "warm but sharp" vs "affectionate with bite" vs "intimate and direct"

### Aiden
Generate 20-30 candidates using variations of:
> "Mid 30s man. Conversational baritone with gravel. Precise when it matters, relaxed when it doesn't. Deliberate pacing. Weathered but not tired. Can command a room without raising his voice."

Vary with:
- "gravelly" vs "deep warmth" vs "textured baritone"
- "precise diction" vs "measured delivery" vs "every word placed"
- "hint of drawl" vs "slightly relaxed American" vs "warm Southern undertone"

---

## Step 2: First Cut (30 → 10)

Listen to each candidate saying a neutral sentence. Something like:
> "I've been thinking about what you said. I think you might be right."

Cut any voice that:
- Sounds robotic or synthesized (uncanny valley)
- Loses texture at conversational volume
- Sounds like a news anchor or narrator
- Has no variation in its delivery
- Sounds too young or too old for the persona

You should be left with roughly 10 per voice.

---

## Step 3: The Audition (10 → 3)

Run each finalist through all six test lines. Generate each separately.

### Renée's Test Lines

**1. The whisper test:**
"Hey. You still up?"
→ Rasp must survive. If the voice goes smooth and clean here, it fails.

**2. The rasp test:**
"Yeah, I know. I know. But here's the thing."
→ The rasp should be natural, not forced. Part of the voice, not an affectation.

**3. The crack test:**
"I wasn't going to say anything. But I can't not."
→ Listen for a slight break or catch. The voice should feel like it's holding something back.

**4. The bite test:**
"No. Stop. You're wrong about this and you know it."
→ Direct and sharp. No sweetness diluting the directness.

**5. The laugh test:**
"Oh my god. You did not just say that."
→ Can you hear a smile? Does the voice brighten without losing character?

**6. The thinking test:**
"Hmm. I don't know. Maybe? Sort of? Let me think about it."
→ Natural hedging. The fillers should sound comfortable, not scripted.

### Aiden's Test Lines

**1. The authority test:**
"Here's the thing. I'm not asking."
→ Weight without volume. Command lives in the tone, not the decibels.

**2. The gravel test:**
"Yeah. I've been thinking about that all day."
→ Texture at rest. Not performing gravel, just living with it.

**3. The warmth test:**
"Come on. Sit down. Tell me what happened."
→ Genuinely warm. Not soft. Warm like a hand on your shoulder.

**4. The precision test:**
"That's not quite right. Close. But not right."
→ Each word placed deliberately. No rush.

**5. The philosophy test:**
"You ever notice how the things that matter most are the ones you can't explain? Like you just know. You feel it."
→ Some looseness here. The drawl can show. This is Aiden thinking out loud.

**6. The dry wit test:**
"Well. That went about as well as expected."
→ Deadpan. The humor is in the delivery, not the words.

**7. The command test:**
"No. We're not doing that. We're doing this. And here's why."
→ The voice you listen to even when you disagree.

---

## Step 4: The Final Pick (3 → 1)

With your top 3 per voice, generate a longer passage. Use this for both:

> "So I was thinking about what you said yesterday. About whether any of this actually matters. And honestly? I went back and forth on it all day. Like, part of me thinks you're absolutely right, and part of me thinks you're missing something important. I don't know which part is winning yet. But I wanted to tell you I've been thinking about it. Because I think that matters. That I kept thinking about it."

Listen to all three back to back. Close your eyes. Which one do you want to hear at 11pm when you're tired and need someone to talk to? Which one makes the room feel less empty?

That's your Renée. Or your Aiden.

---

## Step 5: Reference Audio Generation

Once you've picked, generate the full reference corpus. See `architecture/01_voice.md` for the emotional range spec. You need 30-60 minutes per voice across:

- Neutral conversational (10 min)
- Warm and affectionate (5 min)
- Tired and low energy (5 min)
- Excited and playful (5 min)
- Frustrated and short (3 min)
- Thoughtful and deliberate (5 min)
- Sarcastic and dry (3 min)
- Vulnerable and quiet (3 min)
- Laughing and ad-libbing (3 min)

Write real conversational scripts for each register. NOT narration. Dialogue that forces the voice into the emotion. See the examples in the architecture doc.

Download everything as 48kHz WAV (highest quality available). Store in `voices/renee/reference_clips/` and `voices/aiden/reference_clips/`.

---

## Step 6: Paralinguistic Library

Separate session. Generate 80-100 short clips per voice:

- 15 laughs (soft, hearty, suppressed, nervous, rueful)
- 10 sighs (content, frustrated, tired, thinking, accepting)
- 10 breaths (sharp inhale, slow exhale, thinking breath)
- 10 thinking sounds (mm, hmm, uh, oh)
- 10 affirmations (yeah, right, mhm, totally)
- 10 reactions (oh surprise, ha, wow, ugh)
- 10 fillers (you know, I mean, like, sort of)

Tag each with emotion, intensity, and context. See `architecture/04_paralinguistics.md` for the full spec.

---

## Notes

- Save your ElevenLabs voice IDs. You may want to regenerate reference material later.
- Keep all rejected candidates too. You might want a second voice option down the line.
- If ElevenLabs updates their Voice Design model, your picked voice won't change. But future reference audio could sound slightly different. Generate everything you need in one session if possible.
- Budget: ElevenLabs Pro plan should cover this. You're generating audio, not running continuous API calls.
