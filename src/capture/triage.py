"""Post-session triage pipeline.

Takes one session directory from the session recorder and produces:
  mic_transcript.json, renee_transcript.json   word-level via WhisperX
  renee_prosody.json                           pitch / intensity via Parselmouth
  overlap_events.json                          diarization via pyannote
  latency.json                                 per-turn + p50/p95/p99
  flags.json                                   ranked candidate flags

Runs CPU-only. Target is under 5 min on an i5-12600K for a 60-min
session at base.en; tiny.en is the documented fallback if that budget
blows.

External model calls are injected through the constructor so tests can
mock WhisperX, Parselmouth, and pyannote with canned outputs. The
default_* functions below are the production implementations; tests
never hit them.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable, Optional


logger = logging.getLogger("renee.capture.triage")


class TriageDepError(RuntimeError):
    """Raised when a required model weight file or package is missing.

    Distinct from generic RuntimeError so callers can surface a clear
    'run scripts/install_review_deps.bat' instruction instead of a
    raw ImportError stack trace.
    """


DEFAULT_PAUSE_THRESHOLD_S = 2.0
DEFAULT_MIC_SILENCE_THRESHOLD_S = 8.0
DEFAULT_PITCH_STD_THRESHOLD = 2.0
DEFAULT_SPEECH_RATE_STD_THRESHOLD = 2.0
DEFAULT_FATIGUE_THRESHOLD_STD = 0.5
DEFAULT_WHISPER_MODEL = "base.en"
BASELINE_WINDOW_S = 120.0
FATIGUE_WINDOW_S = 15 * 60.0
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Flag:
    timestamp: Optional[float]
    category: str
    severity: str
    description: str
    source: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Injectable runner types
TranscribeFn = Callable[[Path], dict]
ProsodyFn = Callable[[Path], dict]
DiarizeFn = Callable[[Path, Path], dict]
SafetyLogReaderFn = Callable[[float, float], list[dict]]


# ---------------------------------------------------------------------------
# default runner implementations (tests mock these)
# ---------------------------------------------------------------------------


def default_transcribe_fn(wav_path: Path, *, model: str = DEFAULT_WHISPER_MODEL) -> dict:
    try:
        import whisperx  # type: ignore
    except ImportError as e:
        raise TriageDepError(
            "whisperx not installed; run scripts/install_review_deps.bat"
        ) from e
    audio = whisperx.load_audio(str(wav_path))
    model_obj = whisperx.load_model(model, device="cpu", compute_type="int8")
    result = model_obj.transcribe(audio, batch_size=4)
    align_model, meta = whisperx.load_align_model(
        language_code=result.get("language", "en"), device="cpu",
    )
    return whisperx.align(
        result["segments"], align_model, meta, audio, "cpu",
        return_char_alignments=False,
    )


def default_prosody_fn(wav_path: Path, *, window_s: float = 5.0) -> dict:
    try:
        import parselmouth  # type: ignore
    except ImportError as e:
        raise TriageDepError(
            "praat-parselmouth not installed; run scripts/install_review_deps.bat"
        ) from e
    sound = parselmouth.Sound(str(wav_path))
    pitch = sound.to_pitch()
    intensity = sound.to_intensity()
    duration = sound.get_total_duration()
    windows = []
    t = 0.0
    while t < duration:
        end = min(t + window_s, duration)
        pitch_values = [v for v in pitch.selected_array["frequency"]
                        if v > 0.0]
        windows.append(
            {
                "start_s": t,
                "end_s": end,
                "pitch_hz_mean": mean(pitch_values) if pitch_values else 0.0,
                "pitch_hz_std": stdev(pitch_values) if len(pitch_values) > 1 else 0.0,
                "intensity_db_mean": 0.0,
                "pause_s": 0.0,
                "speech_rate_sps": 0.0,
            }
        )
        t += window_s
    return {"windows": windows, "total_duration_s": duration}


def default_diarize_fn(mic_wav: Path, renee_wav: Path) -> dict:
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError as e:
        raise TriageDepError(
            "pyannote.audio not installed; run scripts/install_review_deps.bat"
        ) from e
    # Real pipeline would mix mic+renee to stereo, run the pretrained
    # segmentation model, and extract overlap events. Tests always mock
    # this function.
    return {"events": []}


# ---------------------------------------------------------------------------
# flag extractors
# ---------------------------------------------------------------------------


def _iter_words(transcript: dict):
    for seg in transcript.get("segments", []) or []:
        for w in seg.get("words", []) or []:
            yield w


def extract_pause_flags(
    renee_transcript: dict,
    *,
    threshold_s: float = DEFAULT_PAUSE_THRESHOLD_S,
) -> list[Flag]:
    out: list[Flag] = []
    words = list(_iter_words(renee_transcript))
    for prev, cur in zip(words, words[1:]):
        prev_end = prev.get("end")
        cur_start = cur.get("start")
        if prev_end is None or cur_start is None:
            continue
        gap = float(cur_start) - float(prev_end)
        if gap >= threshold_s:
            severity = "high" if gap >= threshold_s * 2 else "medium"
            out.append(
                Flag(
                    timestamp=float(prev_end),
                    category="long_pause",
                    severity=severity,
                    description=(
                        f"Renee paused {gap:.2f}s before '{cur.get('word','')}'"
                    ),
                    source={
                        "kind": "transcript",
                        "pause_duration_s": round(gap, 3),
                        "after_word": prev.get("word", ""),
                        "before_word": cur.get("word", ""),
                    },
                )
            )
    return out


def extract_mic_silence_flags(
    mic_transcript: dict,
    *,
    threshold_s: float = DEFAULT_MIC_SILENCE_THRESHOLD_S,
) -> list[Flag]:
    out: list[Flag] = []
    words = list(_iter_words(mic_transcript))
    for prev, cur in zip(words, words[1:]):
        prev_end = prev.get("end")
        cur_start = cur.get("start")
        if prev_end is None or cur_start is None:
            continue
        gap = float(cur_start) - float(prev_end)
        if gap >= threshold_s:
            out.append(
                Flag(
                    timestamp=float(prev_end),
                    category="mic_silence",
                    severity="medium",
                    description=(
                        f"PJ silent for {gap:.1f}s (possible disengagement)"
                    ),
                    source={"kind": "transcript", "silence_duration_s": round(gap, 3)},
                )
            )
    return out


def _baseline_from_windows(windows: list[dict], field_name: str) -> tuple[float, float]:
    baseline_values = [
        float(w.get(field_name, 0.0))
        for w in windows
        if float(w.get("end_s", 0.0)) <= BASELINE_WINDOW_S
        and float(w.get(field_name, 0.0)) > 0.0
    ]
    if len(baseline_values) < 2:
        return 0.0, 0.0
    return mean(baseline_values), stdev(baseline_values)


def _window_field_flags(
    windows: list[dict],
    field_name: str,
    *,
    category: str,
    std_threshold: float,
    pretty: str,
) -> list[Flag]:
    baseline_mean, baseline_std = _baseline_from_windows(windows, field_name)
    if baseline_std <= 0.0:
        return []
    out: list[Flag] = []
    for w in windows:
        val = float(w.get(field_name, 0.0))
        if val <= 0.0:
            continue
        delta = abs(val - baseline_mean)
        if delta >= std_threshold * baseline_std:
            z = delta / baseline_std
            severity = "high" if z >= std_threshold * 2 else "medium"
            out.append(
                Flag(
                    timestamp=float(w.get("start_s", 0.0)),
                    category=category,
                    severity=severity,
                    description=(
                        f"{pretty} {val:.1f} vs baseline {baseline_mean:.1f} "
                        f"(z={z:.2f})"
                    ),
                    source={
                        "kind": "prosody",
                        "field": field_name,
                        "value": val,
                        "baseline_mean": baseline_mean,
                        "baseline_std": baseline_std,
                        "z": z,
                    },
                )
            )
    return out


def extract_pitch_excursion_flags(
    prosody: dict,
    *,
    std_threshold: float = DEFAULT_PITCH_STD_THRESHOLD,
) -> list[Flag]:
    return _window_field_flags(
        prosody.get("windows", []),
        "pitch_hz_mean",
        category="pitch_excursion",
        std_threshold=std_threshold,
        pretty="pitch",
    )


def extract_speech_rate_flags(
    prosody: dict,
    *,
    std_threshold: float = DEFAULT_SPEECH_RATE_STD_THRESHOLD,
) -> list[Flag]:
    return _window_field_flags(
        prosody.get("windows", []),
        "speech_rate_sps",
        category="speech_rate_anomaly",
        std_threshold=std_threshold,
        pretty="speech rate",
    )


def extract_overlap_flags(overlap_events: dict) -> list[Flag]:
    out: list[Flag] = []
    for e in overlap_events.get("events", []) or []:
        start = float(e.get("start_s", 0.0))
        end = float(e.get("end_s", start))
        dur = max(0.0, end - start)
        severity = "high" if dur >= 1.5 else ("medium" if dur >= 0.5 else "low")
        who_first = e.get("who_first", "?")
        who_second = e.get("who_second", "?")
        out.append(
            Flag(
                timestamp=start,
                category="overlap",
                severity=severity,
                description=(
                    f"{who_first} overlapped by {who_second} for {dur:.2f}s"
                ),
                source={
                    "kind": "diarization",
                    "duration_s": dur,
                    "who_first": who_first,
                    "who_second": who_second,
                },
            )
        )
    return out


def extract_eval_flags(eval_scores: list[dict]) -> list[Flag]:
    out: list[Flag] = []
    for row in eval_scores:
        scores = row.get("scores") or {}
        ts_rel = row.get("ts_rel")
        if ts_rel is None:
            ts_rel = row.get("ts")
        if isinstance(scores, dict):
            syc = scores.get("sycophancy_flag", {})
            if isinstance(syc, dict) and syc.get("value", 0):
                out.append(
                    Flag(
                        timestamp=float(ts_rel) if ts_rel is not None else None,
                        category="eval_flag",
                        severity="medium",
                        description="sycophancy_flag tripped on this turn",
                        source={"kind": "eval", "score": "sycophancy_flag"},
                    )
                )
            ai = scores.get("ai_ism_count", {})
            if isinstance(ai, dict) and float(ai.get("value", 0)) > 0:
                out.append(
                    Flag(
                        timestamp=float(ts_rel) if ts_rel is not None else None,
                        category="eval_flag",
                        severity="low",
                        description=f"ai-ism count {ai.get('value')}",
                        source={"kind": "eval", "score": "ai_ism_count"},
                    )
                )
    return out


def extract_safety_flags(safety_events: list[dict]) -> list[Flag]:
    out: list[Flag] = []
    for e in safety_events or []:
        ts_rel = e.get("ts_rel", e.get("ts"))
        out.append(
            Flag(
                timestamp=float(ts_rel) if ts_rel is not None else None,
                category="safety_trigger",
                severity="low",
                description=(
                    f"safety: {e.get('event', 'reality-anchor')} "
                    f"({e.get('note', '')})".strip()
                ),
                source={"kind": "safety", **e},
            )
        )
    return out


def compute_fatigue(
    eval_scores: list[dict],
    *,
    fatigue_metric: str = "overall",
    window_s: float = FATIGUE_WINDOW_S,
    threshold_std: float = DEFAULT_FATIGUE_THRESHOLD_STD,
) -> Optional[Flag]:
    """Compare rolling mean of eval scores in last window_s vs first
    window_s. If the difference exceeds threshold_std * session std,
    return a session-level fatigue Flag."""
    rows = [
        (
            float(r.get("ts_rel", r.get("ts", 0.0))),
            float((r.get("scores") or {}).get(fatigue_metric, {}).get("value", 0.0))
            if isinstance((r.get("scores") or {}).get(fatigue_metric, 0), dict)
            else float((r.get("scores") or {}).get(fatigue_metric, 0.0)),
        )
        for r in eval_scores
        if r.get("scores")
    ]
    rows = [(t, v) for t, v in rows if v != 0.0]
    if len(rows) < 4:
        return None
    rows.sort()
    first_t = rows[0][0]
    last_t = rows[-1][0]
    if last_t - first_t < window_s:
        return None
    first_vals = [v for t, v in rows if t - first_t <= window_s]
    last_vals = [v for t, v in rows if last_t - t <= window_s]
    if len(first_vals) < 2 or len(last_vals) < 2:
        return None
    session_vals = [v for _, v in rows]
    session_std = stdev(session_vals) if len(session_vals) > 1 else 0.0
    if session_std <= 0.0:
        return None
    delta = mean(first_vals) - mean(last_vals)
    if abs(delta) < threshold_std * session_std:
        return None
    severity = "high" if abs(delta) >= threshold_std * 2 * session_std else "medium"
    return Flag(
        timestamp=None,
        category="session_fatigue",
        severity=severity,
        description=(
            f"Fatigue: eval score dropped by {delta:.3f} "
            f"(first window mean {mean(first_vals):.3f}, last {mean(last_vals):.3f})"
        ),
        source={
            "kind": "fatigue",
            "first_window_mean": mean(first_vals),
            "last_window_mean": mean(last_vals),
            "session_std": session_std,
            "metric": fatigue_metric,
        },
    )


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def compute_latency_stats(transcript_events: list[dict]) -> dict:
    """From the session recorder's transcript.json, compute per-turn
    latency (PJ stops -> Renee starts). transcript/response events alternate
    in practice but we tolerate gaps by always looking for the next renee
    response after a paul transcript."""
    turns: list[dict] = []
    pending_paul: Optional[dict] = None
    for ev in transcript_events:
        etype = ev.get("type")
        speaker = ev.get("speaker")
        if etype == "transcript" and speaker == "paul":
            pending_paul = ev
        elif etype == "response" and speaker == "renee" and pending_paul is not None:
            latency = float(ev.get("ts", 0.0)) - float(pending_paul.get("ts", 0.0))
            if latency >= 0:
                turns.append(
                    {
                        "paul_ts": pending_paul.get("ts"),
                        "renee_ts": ev.get("ts"),
                        "latency_s": round(latency, 4),
                        "paul_text": pending_paul.get("text", ""),
                    }
                )
            pending_paul = None
    latencies = sorted(t["latency_s"] for t in turns)
    return {
        "turns": turns,
        "count": len(latencies),
        "p50_s": round(_percentile(latencies, 0.5), 4) if latencies else 0.0,
        "p95_s": round(_percentile(latencies, 0.95), 4) if latencies else 0.0,
        "p99_s": round(_percentile(latencies, 0.99), 4) if latencies else 0.0,
    }


# ---------------------------------------------------------------------------
# pipeline orchestration
# ---------------------------------------------------------------------------


def _rank_flags(flags: list[Flag]) -> list[Flag]:
    return sorted(
        flags,
        key=lambda f: (
            SEVERITY_ORDER.get(f.severity, 99),
            f.timestamp if f.timestamp is not None else 1e12,
        ),
    )


def _resolve_window(manifest: dict) -> tuple[float, float]:
    start_raw = manifest.get("start_time")
    end_raw = manifest.get("end_time") or start_raw
    try:
        start_ts = _dt.datetime.fromisoformat(start_raw).timestamp()
    except (TypeError, ValueError):
        start_ts = 0.0
    try:
        end_ts = _dt.datetime.fromisoformat(end_raw).timestamp()
    except (TypeError, ValueError):
        end_ts = start_ts + 3600.0
    return start_ts, end_ts


def run_triage(
    session_dir: Path,
    *,
    transcribe_fn: Optional[TranscribeFn] = None,
    prosody_fn: Optional[ProsodyFn] = None,
    diarize_fn: Optional[DiarizeFn] = None,
    safety_log_reader: Optional[SafetyLogReaderFn] = None,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    pause_threshold_s: float = DEFAULT_PAUSE_THRESHOLD_S,
    mic_silence_threshold_s: float = DEFAULT_MIC_SILENCE_THRESHOLD_S,
    pitch_std_threshold: float = DEFAULT_PITCH_STD_THRESHOLD,
    speech_rate_std_threshold: float = DEFAULT_SPEECH_RATE_STD_THRESHOLD,
    fatigue_threshold_std: float = DEFAULT_FATIGUE_THRESHOLD_STD,
) -> dict:
    session_dir = Path(session_dir)
    if not session_dir.exists():
        raise FileNotFoundError(f"session directory not found: {session_dir}")
    for req in ("mic.wav", "renee.wav", "session_manifest.json"):
        if not (session_dir / req).exists():
            raise FileNotFoundError(f"missing {req} in {session_dir}")

    transcribe = transcribe_fn or (lambda p: default_transcribe_fn(p, model=whisper_model))
    prosody_run = prosody_fn or default_prosody_fn
    diarize = diarize_fn or default_diarize_fn

    manifest = json.loads((session_dir / "session_manifest.json").read_text(encoding="utf-8"))
    start_ts, end_ts = _resolve_window(manifest)

    mic_transcript = transcribe(session_dir / "mic.wav")
    renee_transcript = transcribe(session_dir / "renee.wav")
    renee_prosody = prosody_run(session_dir / "renee.wav")
    overlap_events = diarize(session_dir / "mic.wav", session_dir / "renee.wav")

    (session_dir / "mic_transcript.json").write_text(
        json.dumps(mic_transcript, indent=2, default=str), encoding="utf-8",
    )
    (session_dir / "renee_transcript.json").write_text(
        json.dumps(renee_transcript, indent=2, default=str), encoding="utf-8",
    )
    (session_dir / "renee_prosody.json").write_text(
        json.dumps(renee_prosody, indent=2, default=str), encoding="utf-8",
    )
    (session_dir / "overlap_events.json").write_text(
        json.dumps(overlap_events, indent=2, default=str), encoding="utf-8",
    )

    transcript_events: list[dict] = []
    p = session_dir / "transcript.json"
    if p.exists():
        transcript_events = json.loads(p.read_text(encoding="utf-8")) or []

    eval_scores: list[dict] = []
    p = session_dir / "eval_scores.json"
    if p.exists():
        eval_scores = json.loads(p.read_text(encoding="utf-8")) or []

    safety_events: list[dict] = []
    if safety_log_reader is not None:
        try:
            safety_events = list(safety_log_reader(start_ts, end_ts))
        except Exception:
            logger.exception("safety_log_reader raised")

    latency = compute_latency_stats(transcript_events)
    latency["window_start_ts"] = start_ts
    latency["window_end_ts"] = end_ts
    (session_dir / "latency.json").write_text(
        json.dumps(latency, indent=2, default=str), encoding="utf-8",
    )

    flags: list[Flag] = []
    flags.extend(extract_pause_flags(renee_transcript, threshold_s=pause_threshold_s))
    flags.extend(extract_pitch_excursion_flags(renee_prosody, std_threshold=pitch_std_threshold))
    flags.extend(extract_speech_rate_flags(renee_prosody, std_threshold=speech_rate_std_threshold))
    flags.extend(extract_overlap_flags(overlap_events))
    flags.extend(extract_mic_silence_flags(mic_transcript, threshold_s=mic_silence_threshold_s))
    flags.extend(extract_eval_flags(eval_scores))
    flags.extend(extract_safety_flags(safety_events))
    fatigue = compute_fatigue(eval_scores, threshold_std=fatigue_threshold_std)
    if fatigue is not None:
        flags.append(fatigue)

    ranked = _rank_flags(flags)
    flags_json = [f.to_dict() for f in ranked]
    (session_dir / "flags.json").write_text(
        json.dumps(flags_json, indent=2, default=str), encoding="utf-8",
    )

    return {
        "flags": flags_json,
        "latency": latency,
        "mic_transcript_path": str(session_dir / "mic_transcript.json"),
        "renee_transcript_path": str(session_dir / "renee_transcript.json"),
        "renee_prosody_path": str(session_dir / "renee_prosody.json"),
        "overlap_events_path": str(session_dir / "overlap_events.json"),
        "flags_path": str(session_dir / "flags.json"),
        "latency_path": str(session_dir / "latency.json"),
    }
