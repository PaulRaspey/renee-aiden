# Triage pipeline manual validation

Tests for `src/capture/triage.py` mock WhisperX, Parselmouth, and pyannote
with canned outputs so they never hit real model weights. That keeps CI
deterministic and fast but means the first end-to-end run against real
session audio is a manual job PJ does once the review deps are installed.

Run through this checklist after the first real session is captured.

## Prerequisites

1. `scripts/install_review_deps.bat` completed without warnings:
   - whisperx, praat-parselmouth, pyannote.audio, matplotlib, plotly all
     report OK in `python -m src.capture.review_deps status`.
   - ffmpeg is on PATH.
   - `HF_TOKEN` (or `HUGGING_FACE_HUB_TOKEN`) is set in the shell that
     runs triage, and the pyannote terms have been accepted at
     <https://huggingface.co/pyannote/speaker-diarization-3.1>.
2. At least one session under `%RENEE_SESSIONS_DIR%` (default
   `C:\Users\Epsar\renee-sessions\`) has both `mic.wav` and `renee.wav`
   present.

## Run the pipeline

```
cd C:\Users\Epsar\Desktop\renee-aiden
.venv\Scripts\activate
python -m renee triage "C:\Users\Epsar\renee-sessions\2026-04-21T19-30-00"
```

Expected wall-clock on a 60-min session at base.en on the OptiPlex:
under 5 minutes. If it exceeds that by more than 50%, drop to
`--whisper-model tiny.en` and note the downgrade in
`state/triage_timing.log`.

## Per-pass checks

For each pass, open the generated JSON and confirm:

### Transcription

- [ ] `mic_transcript.json` exists and contains at least one segment with
      word-level timestamps (each word has `start` and `end` keys).
- [ ] `renee_transcript.json` same shape.
- [ ] Word timestamps are monotonically non-decreasing within a segment.
- [ ] Hand-spot-check one 30-second window: read the transcript and
      confirm the words roughly match what you remember saying.

### Prosody

- [ ] `renee_prosody.json` has a `windows` list covering the full session
      duration.
- [ ] Each window has `pitch_hz_mean` > 0 during renee speech and
      `intensity_db_mean` plausibly in the 40..80 dB range.
- [ ] Pause durations look right for a 5s window covering a known pause.

### Overlap

- [ ] `overlap_events.json` has an `events` list.
- [ ] If you remember talking over each other, a corresponding event
      exists with `start_s` close to the wall-clock moment.

### Latency

- [ ] `latency.json` `count` matches the number of turn-taking events you
      remember.
- [ ] `p50_s`, `p95_s`, `p99_s` are present and non-negative.
- [ ] If `p95_s` > 2.0 seconds, that's a drift signal; log it.

### Flags

- [ ] `flags.json` is a list (possibly empty for a very clean session).
- [ ] Flags are sorted high-to-low severity, then by timestamp.
- [ ] Open the dashboard Sessions tab; each flag in `flags.json` has a
      corresponding marker on the waveform.

## Regression spot-check

Compare run output against the synthetic fixture numbers from
`tests/test_capture_triage.py`:

- A planted 2.4s pause should produce exactly one `long_pause` flag with
  `severity == "medium"`.
- A planted pitch excursion at window index 10 should produce exactly one
  `pitch_excursion` flag with `severity in ("medium", "high")`.
- A planted overlap event with `duration_s == 1.8` should produce one
  `overlap` flag with `severity == "high"`.

If the production output disagrees on category, severity, or count for
equivalent planted anomalies, the mocks have drifted from reality. File
a bug under the capture pipeline and block further sessions until the
mocks are refit.

## Failure modes to watch

- **Stack trace from pyannote about "private model"** → terms not
  accepted on HuggingFace, or token lacks read permission. Accept the
  terms and regenerate the token.
- **"Out of memory" on alignment** → drop to tiny.en. If that persists,
  the alignment model's VRAM budget on CPU mode is unusual; check
  `COMPUTE_TYPE=int8` is being passed.
- **Empty renee_transcript.json** → renee.wav has no speech, likely a
  stuck TTS or the session ended before renee said anything. Check
  `transcript.json` for matching response events.
- **Extremely long pauses flagged throughout** → Parselmouth disagrees
  with WhisperX on word boundaries. Expected to surface occasionally;
  follow up if more than 20% of flags on a clean session are false
  positives.

## Logging the result

After triage completes, append one line to
`state/triage_timing.log`:

```
2026-04-21 <session_id> <wall_seconds> <flag_count> <whisper_model>
```

This gives PJ the historical wall-clock trend without a new metrics
table. Rotate the file monthly if it exceeds 1 MB.
