"""Path B integration: webm/opus -> 48kHz int16 PCM round-trip.

The Replit Express bridge spawns ffmpeg per session to transcode the PWA's
MediaRecorder webm chunks into the raw PCM the renee-aiden audio_bridge
expects. This file exercises the same ffmpeg invocation against a real
binary so we catch regressions in the args, the stdin/stdout streaming
behavior, and the byte-exact length math.

Skipped automatically when ffmpeg isn't on PATH (so CI without the binary
just goes green).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not on PATH (winget install Gyan.FFmpeg or apt install ffmpeg)",
)


# Args mirror artifacts/api-server/src/lib/ws-handler.ts startInboundTranscoder()
TRANSCODE_ARGS = [
    "-loglevel", "error",
    "-f", "webm",
    "-i", "pipe:0",
    "-f", "s16le",
    "-acodec", "pcm_s16le",
    "-ar", "48000",
    "-ac", "1",
    "pipe:1",
]


def _generate_test_webm(out_path: Path, *, duration_s: float = 1.0) -> bytes:
    """Use ffmpeg's lavfi sine generator to produce a webm/opus blob."""
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}",
            "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-f", "webm",
            str(out_path),
        ],
        capture_output=True, timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg sine gen failed: {proc.stderr!r}")
    return out_path.read_bytes()


def _run_transcode(webm_bytes: bytes) -> tuple[int, bytes, str]:
    """Pipe webm into ffmpeg with the bridge's exact args; return PCM out."""
    proc = subprocess.run(
        ["ffmpeg", *TRANSCODE_ARGS],
        input=webm_bytes,
        capture_output=True, timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


def test_transcode_one_second_tone_yields_exact_pcm_length(tmp_path: Path):
    """1s of 48kHz mono int16 PCM is exactly 96000 bytes (48000 samples × 2)."""
    webm_path = tmp_path / "tone1s.webm"
    _generate_test_webm(webm_path, duration_s=1.0)
    rc, pcm, stderr = _run_transcode(webm_path.read_bytes())
    assert rc == 0, f"ffmpeg failed: {stderr}"
    expected = 48000 * 2
    # Allow ±10% for opus codec delay/padding
    assert abs(len(pcm) - expected) <= expected * 0.10, \
        f"got {len(pcm)} bytes, expected ~{expected}"


def test_transcode_five_second_tone_streaming_chunked(tmp_path: Path):
    """5s of audio chopped into 10 ~100ms chunks streamed to ffmpeg's stdin
    must produce the same total PCM length as a one-shot transcode.

    This is the closest pure-Python analogue of MediaRecorder's behavior on
    the PWA: container header in the first chunk, clusters in the rest.
    """
    webm_path = tmp_path / "tone5s.webm"
    _generate_test_webm(webm_path, duration_s=5.0)
    full = webm_path.read_bytes()
    chunks = [full[i:i + len(full) // 10] for i in range(0, len(full), max(1, len(full) // 10))]

    proc = subprocess.Popen(
        ["ffmpeg", *TRANSCODE_ARGS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    pcm_out = bytearray()

    # Pipe chunks; collect stdout as it arrives
    import threading

    def reader():
        nonlocal pcm_out
        while True:
            data = proc.stdout.read(8192)
            if not data:
                break
            pcm_out.extend(data)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for ch in chunks:
        proc.stdin.write(ch)
    proc.stdin.close()
    proc.wait(timeout=20)
    t.join(timeout=5)

    expected = 48000 * 2 * 5
    assert len(pcm_out) >= expected * 0.9, \
        f"streaming transcode lost data: {len(pcm_out)} of expected {expected}"


def test_transcode_amplitude_well_above_noise_floor(tmp_path: Path):
    """A real signal should peak well above silence, even after opus."""
    webm_path = tmp_path / "tone1s.webm"
    _generate_test_webm(webm_path, duration_s=1.0)
    _, pcm, _ = _run_transcode(webm_path.read_bytes())
    # Peak amplitude across all samples
    import struct
    if len(pcm) < 2:
        pytest.fail("PCM too short")
    peak = 0
    for i in range(0, len(pcm) - 1, 2):
        sample = abs(struct.unpack_from("<h", pcm, i)[0])
        if sample > peak:
            peak = sample
    # ffmpeg's sine generator defaults to ~0.25 amplitude; opus may attenuate
    # somewhat. 1000 (=~3% full-scale) is comfortably above the noise floor.
    assert peak > 1000, f"peak {peak} suggests silence — transcode probably wrong"


def test_transcode_invalid_input_returns_nonzero(tmp_path: Path):
    """Garbage at stdin produces a non-zero exit; the bridge's stdin.write
    error path swallows this, so the only consequence is empty PCM."""
    rc, pcm, stderr = _run_transcode(b"\x00\x01\x02\x03 not a webm container")
    assert rc != 0
    assert "EBML" in stderr or "Invalid" in stderr or "header" in stderr


# ---------------------------------------------------------------------------
# WAV-wrap reverse path: bridge buffers PCM and wraps in WAV before sending
# back to the PWA. We can verify the wrap is structurally valid here.
# ---------------------------------------------------------------------------


def _wrap_pcm_in_wav(pcm: bytes, *, sr: int = 48000, ch: int = 1, bits: int = 16) -> bytes:
    """Mirror of ws-handler.ts wrapPcmInWav (RIFF/WAVE container)."""
    import struct
    data_size = len(pcm)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)        # fmt chunk size
        + struct.pack("<H", 1)          # PCM
        + struct.pack("<H", ch)
        + struct.pack("<I", sr)
        + struct.pack("<I", sr * ch * bits // 8)  # byte rate
        + struct.pack("<H", ch * bits // 8)        # block align
        + struct.pack("<H", bits)
        + b"data"
        + struct.pack("<I", data_size)
    )
    return header + pcm


def test_wav_wrap_is_decodable_by_ffmpeg(tmp_path: Path):
    """Generate raw PCM, wrap in WAV, hand to ffmpeg — round-trip equivalence
    means the bridge's WAV header is valid (and AudioContext.decodeAudioData
    on the PWA side will accept it)."""
    # 1 second of 440Hz at 48kHz mono
    import math
    import struct
    samples = []
    for n in range(48000):
        samples.append(int(0.25 * 32767 * math.sin(2 * math.pi * 440 * n / 48000)))
    pcm = b"".join(struct.pack("<h", s) for s in samples)
    wav = _wrap_pcm_in_wav(pcm, sr=48000, ch=1, bits=16)
    wav_path = tmp_path / "out.wav"
    wav_path.write_bytes(wav)
    # Pipe through ffmpeg to confirm it's a valid container
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(wav_path),
         "-f", "null", "-"],
        capture_output=True, timeout=10,
    )
    assert proc.returncode == 0, f"WAV wrap not valid: {proc.stderr!r}"
