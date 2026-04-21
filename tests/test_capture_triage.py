"""Tests for the post-session triage pipeline.

All external model calls are mocked. No real WhisperX, Parselmouth, or
pyannote weights are loaded. Each test plants a known anomaly and asserts
the flag generator surfaces it with the expected category/severity.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from src.capture import triage
from src.capture.triage import (
    DEFAULT_PAUSE_THRESHOLD_S,
    Flag,
    TriageDepError,
    compute_fatigue,
    compute_latency_stats,
    extract_eval_flags,
    extract_mic_silence_flags,
    extract_overlap_flags,
    extract_pause_flags,
    extract_pitch_excursion_flags,
    extract_safety_flags,
    extract_speech_rate_flags,
    run_triage,
)


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------


def _write_silence_wav(path: Path, seconds: float = 60.0) -> None:
    n = int(48000 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(bytes(n * 2))


def _make_manifest(session_id: str, duration_s: float = 60.0) -> dict:
    return {
        "session_id": session_id,
        "start_time": "2026-04-21T19:30:00+00:00",
        "end_time": "2026-04-21T19:31:00+00:00",
        "renee_versions": {},
        "backend_used": "cascade",
        "pod_id": None,
        "starter_metadata": {},
        "public": False,
        "reviewed": False,
        "github_published": False,
        "presence_score": None,
        "notes_file": "",
        "genesis_session": True,
        "memory_snapshot": {},
    }


def _make_session(
    tmp_path: Path,
    *,
    transcript_events: list | None = None,
    eval_scores: list | None = None,
) -> Path:
    session_id = "2026-04-21T19-30-00"
    session_dir = tmp_path / session_id
    session_dir.mkdir(parents=True)
    _write_silence_wav(session_dir / "mic.wav")
    _write_silence_wav(session_dir / "renee.wav")
    (session_dir / "session_manifest.json").write_text(
        json.dumps(_make_manifest(session_id)), encoding="utf-8",
    )
    (session_dir / "transcript.json").write_text(
        json.dumps(transcript_events or []), encoding="utf-8",
    )
    (session_dir / "eval_scores.json").write_text(
        json.dumps(eval_scores or []), encoding="utf-8",
    )
    return session_dir


def _plain_transcribe(_path: Path) -> dict:
    return {
        "segments": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "hello",
                "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
            },
        ],
        "language": "en",
    }


def _plain_prosody(_path: Path) -> dict:
    return {
        "windows": [
            {
                "start_s": float(i * 5),
                "end_s": float(i * 5 + 5),
                "pitch_hz_mean": 180.0,
                "pitch_hz_std": 5.0,
                "intensity_db_mean": 60.0,
                "pause_s": 0.1,
                "speech_rate_sps": 3.0,
            }
            for i in range(12)
        ],
        "total_duration_s": 60.0,
    }


def _plain_diarize(_mic: Path, _renee: Path) -> dict:
    return {"events": []}


# ---------------------------------------------------------------------------
# dep-missing behaviour
# ---------------------------------------------------------------------------


def test_missing_whisperx_produces_clear_error(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        if name == "whisperx":
            raise ImportError("simulated missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(TriageDepError) as ei:
        triage.default_transcribe_fn(Path("mic.wav"))
    assert "scripts/install_review_deps.bat" in str(ei.value)


def test_missing_parselmouth_produces_clear_error(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        if name == "parselmouth":
            raise ImportError("simulated missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(TriageDepError) as ei:
        triage.default_prosody_fn(Path("renee.wav"))
    assert "install_review_deps" in str(ei.value)


def test_missing_pyannote_produces_clear_error(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        if name == "pyannote.audio":
            raise ImportError("simulated missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(TriageDepError):
        triage.default_diarize_fn(Path("mic.wav"), Path("renee.wav"))


# ---------------------------------------------------------------------------
# individual extractors
# ---------------------------------------------------------------------------


def test_extract_pause_flags_identifies_planted_pause():
    transcript = {
        "segments": [
            {
                "start": 0.0,
                "end": 5.0,
                "text": "hello the Alps",
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.5},
                    {"word": "the", "start": 2.9, "end": 3.1},
                    {"word": "Alps", "start": 3.2, "end": 3.5},
                ],
            },
        ],
    }
    flags = extract_pause_flags(transcript)
    assert len(flags) == 1
    f = flags[0]
    assert f.category == "long_pause"
    assert f.severity == "medium"
    assert f.timestamp == pytest.approx(0.5)
    assert f.source["pause_duration_s"] == pytest.approx(2.4, abs=0.01)


def test_extract_pause_flags_no_flag_under_threshold():
    transcript = {
        "segments": [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "hi there",
                "words": [
                    {"word": "hi", "start": 0.0, "end": 0.3},
                    {"word": "there", "start": 1.0, "end": 1.3},
                ],
            },
        ],
    }
    assert extract_pause_flags(transcript) == []


def test_extract_pause_flags_high_severity_for_very_long_pause():
    transcript = {
        "segments": [
            {
                "start": 0.0,
                "end": 10.0,
                "text": "one two",
                "words": [
                    {"word": "one", "start": 0.0, "end": 0.2},
                    {"word": "two", "start": 6.0, "end": 6.2},
                ],
            },
        ],
    }
    flags = extract_pause_flags(transcript)
    assert len(flags) == 1
    assert flags[0].severity == "high"


def test_extract_pitch_excursion_flags():
    prosody = {
        "windows": [
            {"start_s": i * 5.0, "end_s": i * 5.0 + 5.0,
             "pitch_hz_mean": 180.0, "pitch_hz_std": 1.0,
             "intensity_db_mean": 60.0, "pause_s": 0.0, "speech_rate_sps": 3.0}
            for i in range(24)
        ],
    }
    prosody["windows"][20]["pitch_hz_mean"] = 240.0
    flags = extract_pitch_excursion_flags(prosody)
    assert len(flags) == 1
    assert flags[0].category == "pitch_excursion"
    assert flags[0].severity in ("medium", "high")
    assert flags[0].timestamp == pytest.approx(100.0)


def test_extract_pitch_excursion_no_flag_on_stable_pitch():
    prosody = {
        "windows": [
            {"start_s": i * 5.0, "end_s": i * 5.0 + 5.0,
             "pitch_hz_mean": 180.0 + (i % 2),
             "pitch_hz_std": 1.0, "intensity_db_mean": 60.0,
             "pause_s": 0.0, "speech_rate_sps": 3.0}
            for i in range(24)
        ],
    }
    assert extract_pitch_excursion_flags(prosody) == []


def test_extract_overlap_flags_severity_scales_with_duration():
    overlap = {
        "events": [
            {"start_s": 5.0, "end_s": 5.2, "who_first": "renee", "who_second": "paul"},
            {"start_s": 10.0, "end_s": 10.8, "who_first": "renee", "who_second": "paul"},
            {"start_s": 20.0, "end_s": 22.0, "who_first": "renee", "who_second": "paul"},
        ],
    }
    flags = extract_overlap_flags(overlap)
    assert len(flags) == 3
    assert flags[0].severity == "low"
    assert flags[1].severity == "medium"
    assert flags[2].severity == "high"


def test_extract_mic_silence_flags_over_threshold():
    mic = {
        "segments": [
            {
                "start": 0.0,
                "end": 30.0,
                "text": "one two",
                "words": [
                    {"word": "one", "start": 0.0, "end": 0.3},
                    {"word": "two", "start": 12.0, "end": 12.3},
                ],
            },
        ],
    }
    flags = extract_mic_silence_flags(mic)
    assert len(flags) == 1
    assert flags[0].category == "mic_silence"
    assert flags[0].source["silence_duration_s"] == pytest.approx(11.7, abs=0.01)


def test_extract_eval_flags_surface_scores():
    eval_scores = [
        {
            "ts_rel": 12.3,
            "scores": {
                "sycophancy_flag": {"value": 1},
                "ai_ism_count": {"value": 2},
            },
        },
    ]
    flags = extract_eval_flags(eval_scores)
    categories = [f.category for f in flags]
    assert categories.count("eval_flag") == 2


def test_extract_safety_flags_low_severity():
    safety = [{"ts_rel": 45.0, "event": "reality_anchor", "note": "neutral turn"}]
    flags = extract_safety_flags(safety)
    assert len(flags) == 1
    assert flags[0].category == "safety_trigger"
    assert flags[0].severity == "low"


# ---------------------------------------------------------------------------
# fatigue
# ---------------------------------------------------------------------------


def test_compute_fatigue_flags_planted_score_decay():
    first_block = [
        {"ts_rel": float(t), "scores": {"overall": {"value": 0.95}}}
        for t in range(0, 15 * 60 + 1, 60)
    ]
    last_block = [
        {"ts_rel": float(t), "scores": {"overall": {"value": 0.5}}}
        for t in range(45 * 60, 60 * 60 + 1, 60)
    ]
    flag = compute_fatigue(first_block + last_block, fatigue_metric="overall")
    assert flag is not None
    assert flag.category == "session_fatigue"
    assert flag.severity in ("medium", "high")


def test_compute_fatigue_no_flag_on_stable_scores():
    rows = [
        {"ts_rel": float(t), "scores": {"overall": {"value": 0.8}}}
        for t in range(0, 60 * 60, 60)
    ]
    assert compute_fatigue(rows, fatigue_metric="overall") is None


def test_compute_fatigue_none_when_too_few_points():
    rows = [{"ts_rel": 0.0, "scores": {"overall": {"value": 0.8}}}]
    assert compute_fatigue(rows, fatigue_metric="overall") is None


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------


def test_compute_latency_percentiles_for_known_turns():
    events = []
    for i, lat in enumerate([0.4, 0.6, 0.8, 1.2, 2.0]):
        base = float(i * 10)
        events.append({"ts": base, "type": "transcript", "speaker": "paul", "text": "q"})
        events.append({"ts": base + lat, "type": "response", "speaker": "renee", "text": "a"})
    latency = compute_latency_stats(events)
    assert latency["count"] == 5
    assert latency["p50_s"] == pytest.approx(0.8)
    assert latency["p95_s"] == pytest.approx(1.84, abs=0.01)
    assert latency["p99_s"] == pytest.approx(1.9680, abs=0.01)


def test_compute_latency_empty():
    latency = compute_latency_stats([])
    assert latency["count"] == 0
    assert latency["p50_s"] == 0.0


# ---------------------------------------------------------------------------
# end-to-end runs
# ---------------------------------------------------------------------------


def test_end_to_end_clean_session_empty_flags(tmp_path):
    session_dir = _make_session(tmp_path)
    result = run_triage(
        session_dir,
        transcribe_fn=_plain_transcribe,
        prosody_fn=_plain_prosody,
        diarize_fn=_plain_diarize,
    )
    assert (session_dir / "mic_transcript.json").exists()
    assert (session_dir / "renee_transcript.json").exists()
    assert (session_dir / "renee_prosody.json").exists()
    assert (session_dir / "overlap_events.json").exists()
    assert (session_dir / "latency.json").exists()
    assert (session_dir / "flags.json").exists()
    flags = json.loads((session_dir / "flags.json").read_text())
    assert flags == []
    assert result["latency"]["count"] == 0


def test_end_to_end_planted_anomalies_flagged(tmp_path):
    transcript_events = []
    for i, lat in enumerate([0.5, 0.6]):
        transcript_events.append(
            {"ts": float(i * 10), "type": "transcript", "speaker": "paul", "text": "q"},
        )
        transcript_events.append(
            {"ts": float(i * 10) + lat, "type": "response", "speaker": "renee", "text": "a"},
        )
    session_dir = _make_session(tmp_path, transcript_events=transcript_events)

    def planted_transcribe(path: Path) -> dict:
        if "renee" in path.name:
            return {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 5.0,
                        "text": "hello the Alps",
                        "words": [
                            {"word": "hello", "start": 0.0, "end": 0.5},
                            {"word": "the", "start": 2.9, "end": 3.1},
                            {"word": "Alps", "start": 3.2, "end": 3.5},
                        ],
                    },
                ],
            }
        return _plain_transcribe(path)

    def planted_prosody(path: Path) -> dict:
        data = _plain_prosody(path)
        data["windows"][-1]["pitch_hz_mean"] = 260.0
        return data

    def planted_diarize(mic: Path, renee: Path) -> dict:
        return {
            "events": [
                {"start_s": 15.0, "end_s": 16.8, "who_first": "renee", "who_second": "paul"},
            ],
        }

    def planted_safety_log(start_ts: float, end_ts: float) -> list[dict]:
        return [{"ts_rel": 22.0, "event": "reality_anchor", "note": "neutral turn"}]

    result = run_triage(
        session_dir,
        transcribe_fn=planted_transcribe,
        prosody_fn=planted_prosody,
        diarize_fn=planted_diarize,
        safety_log_reader=planted_safety_log,
    )
    categories = {f["category"] for f in result["flags"]}
    assert "long_pause" in categories
    assert "overlap" in categories
    assert "safety_trigger" in categories


def test_end_to_end_ranks_flags_high_first(tmp_path):
    session_dir = _make_session(tmp_path)

    def many_overlaps(_mic: Path, _renee: Path) -> dict:
        return {
            "events": [
                {"start_s": 20.0, "end_s": 20.2, "who_first": "renee", "who_second": "paul"},
                {"start_s": 5.0, "end_s": 7.5, "who_first": "renee", "who_second": "paul"},
            ],
        }

    result = run_triage(
        session_dir,
        transcribe_fn=_plain_transcribe,
        prosody_fn=_plain_prosody,
        diarize_fn=many_overlaps,
    )
    severities = [f["severity"] for f in result["flags"]]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


def test_run_triage_missing_wav_raises(tmp_path):
    session_dir = tmp_path / "empty"
    session_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        run_triage(
            session_dir,
            transcribe_fn=_plain_transcribe,
            prosody_fn=_plain_prosody,
            diarize_fn=_plain_diarize,
        )


def test_flags_sorted_by_severity_then_timestamp(tmp_path):
    session_dir = _make_session(tmp_path)

    def mixed_overlap(_mic, _renee):
        return {
            "events": [
                {"start_s": 50.0, "end_s": 50.3, "who_first": "r", "who_second": "p"},
                {"start_s": 20.0, "end_s": 20.2, "who_first": "r", "who_second": "p"},
                {"start_s": 10.0, "end_s": 12.5, "who_first": "r", "who_second": "p"},
            ],
        }

    result = run_triage(
        session_dir,
        transcribe_fn=_plain_transcribe,
        prosody_fn=_plain_prosody,
        diarize_fn=mixed_overlap,
    )
    high = [f for f in result["flags"] if f["severity"] == "high"]
    lows = [f for f in result["flags"] if f["severity"] == "low"]
    assert high and lows
    assert result["flags"].index(high[0]) < result["flags"].index(lows[0])


def test_triage_validation_doc_exists():
    path = Path(__file__).resolve().parent.parent / "docs" / "triage_validation.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "manual validation" in content.lower()
    assert "install_review_deps" in content
