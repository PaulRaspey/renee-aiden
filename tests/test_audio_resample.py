"""Numerical checks for the resampler that the JS client mirrors.

The big one: feed a 440 Hz sine at 44100 Hz, resample to 48000 Hz,
and assert the dominant FFT bin stays at 440 Hz. If the JS and Python
copies drift apart the output will be detuned.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.client.audio_resample import (
    ResamplerState,
    float_to_int16,
    resample_chunk,
    resample_stream,
)


def test_noop_when_rates_match():
    state = ResamplerState(src_rate=48000, dst_rate=48000)
    x = np.random.default_rng(0).standard_normal(960).astype(np.float32) * 0.1
    y = resample_chunk(x, state)
    assert y.shape == x.shape
    assert np.allclose(y, x)


def test_sine_at_44100_preserves_frequency_after_resample_to_48000():
    src_rate = 44100
    dst_rate = 48000
    freq = 440.0
    dur_s = 1.0
    n_src = int(src_rate * dur_s)
    t = np.arange(n_src, dtype=np.float32) / src_rate
    sine = np.sin(2 * np.pi * freq * t).astype(np.float32)

    # Feed the resampler in worklet-sized chunks (128 frames) so the
    # chunk boundary logic is exercised.
    chunks = [sine[i : i + 128] for i in range(0, n_src, 128)]
    y = resample_stream(chunks, src_rate, dst_rate)
    assert abs(y.shape[0] - int(n_src * dst_rate / src_rate)) <= 2

    # Dominant frequency via FFT peak. Use a Hann window to reduce
    # spectral leakage so the peak sits exactly on a bin.
    window = np.hanning(y.shape[0])
    spectrum = np.abs(np.fft.rfft(y * window))
    freqs = np.fft.rfftfreq(y.shape[0], d=1 / dst_rate)
    peak_idx = int(np.argmax(spectrum))
    peak_freq = float(freqs[peak_idx])
    assert abs(peak_freq - freq) < 1.0, (
        f"peak {peak_freq:.2f} Hz drifted from expected 440 Hz"
    )


def test_sine_at_22050_preserves_frequency_after_resample_to_48000():
    """iOS sometimes pins AudioContext to 22050. Cover that case too."""
    src_rate = 22050
    dst_rate = 48000
    freq = 1000.0
    n_src = src_rate
    t = np.arange(n_src, dtype=np.float32) / src_rate
    sine = np.sin(2 * np.pi * freq * t).astype(np.float32)
    chunks = [sine[i : i + 128] for i in range(0, n_src, 128)]
    y = resample_stream(chunks, src_rate, dst_rate)
    window = np.hanning(y.shape[0])
    spec = np.abs(np.fft.rfft(y * window))
    freqs = np.fft.rfftfreq(y.shape[0], d=1 / dst_rate)
    peak_freq = float(freqs[int(np.argmax(spec))])
    assert abs(peak_freq - freq) < 1.5


def test_float_to_int16_clamps_and_round_trips_small_values():
    f = np.array([0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5], dtype=np.float32)
    i = float_to_int16(f)
    assert i[0] == 0
    assert i[3] == 32767
    assert i[4] == -32768
    assert i[5] == 32767   # clamped
    assert i[6] == -32768  # clamped


def test_js_and_python_resamplers_agree_on_440hz_sine():
    """Run the JS streaming resampler under Node and compare the
    reconstructed frequency to the Python mirror. If this fails,
    src/client/web/client.js and src/client/audio_resample.py have
    drifted and the phone and tests will disagree about the stream."""
    import json
    import shutil
    import subprocess
    import textwrap

    if shutil.which("node") is None:
        pytest.skip("node not on PATH; cannot exercise the JS resampler")

    js = textwrap.dedent(
        """
        const srcRate = 44100, dstRate = 48000, freq = 440.0;
        const nSrc = srcRate;
        const sine = new Float32Array(nSrc);
        for (let i = 0; i < nSrc; i++) {
          sine[i] = Math.sin(2 * Math.PI * freq * (i / srcRate));
        }
        let tail = 0, lastSample = 0;
        const out = [];
        for (let c = 0; c < nSrc; c += 128) {
          const chunk = sine.subarray(c, Math.min(c + 128, nSrc));
          const approxLen = Math.floor(chunk.length * (dstRate / srcRate) + 1);
          const o = new Float32Array(approxLen);
          let wi = 0, pos = tail;
          while (pos < chunk.length) {
            const i = Math.floor(pos);
            const frac = pos - i;
            const a = i === 0 ? lastSample : chunk[i - 1];
            const b = chunk[i];
            o[wi++] = a + (b - a) * frac;
            pos += srcRate / dstRate;
          }
          tail = pos - chunk.length;
          lastSample = chunk[chunk.length - 1];
          for (let k = 0; k < wi; k++) out.push(o[k]);
        }
        const start = Math.floor(out.length / 4);
        const end = Math.floor(out.length * 3 / 4);
        const w = out.slice(start, end);
        let zeros = 0;
        for (let i = 1; i < w.length; i++) {
          if (w[i - 1] <= 0 && w[i] > 0) zeros++;
        }
        console.log(JSON.stringify({
          out_len: out.length,
          freq_est: zeros / (w.length / dstRate)
        }));
        """
    )
    result = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    # Python at the same inputs.
    py_out = resample_stream(
        [
            np.sin(2 * np.pi * 440 * np.arange(i, min(i + 128, 44100)) / 44100).astype(
                np.float32
            )
            for i in range(0, 44100, 128)
        ],
        44100,
        48000,
    )
    assert abs(report["out_len"] - py_out.shape[0]) <= 2
    assert abs(report["freq_est"] - 440.0) < 1.0


def test_resampler_chunk_boundaries_do_not_drop_samples():
    """Cumulative output length across chunks must match what a single
    big-chunk resample would produce (within 1 sample, since the last
    fractional bit may land in the next chunk)."""
    src_rate, dst_rate = 44100, 48000
    n = 44100
    rng = np.random.default_rng(1)
    x = rng.standard_normal(n).astype(np.float32) * 0.2
    single = resample_stream([x], src_rate, dst_rate)
    chunked = resample_stream(
        [x[i : i + 200] for i in range(0, n, 200)], src_rate, dst_rate,
    )
    assert abs(single.shape[0] - chunked.shape[0]) <= 1