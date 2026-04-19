"""Streaming linear resampler that mirrors the JS implementation in
``src/client/web/client.js``.

The mobile PWA captures microphone audio at whatever sample rate the
browser's ``AudioContext`` exposes; on iOS that is often the device
hardware rate (44100 or 22050) rather than the 48000 we ship to the
bridge. The JS worklet runs the same linear interpolation below and
converts to int16 LE before sending over the WebSocket.

This Python copy exists so we can write deterministic tests for the
algorithm. The JS must stay in sync; both files are short enough that
a diff review catches drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np


@dataclass
class ResamplerState:
    src_rate: int
    dst_rate: int
    tail: float = 0.0
    last_sample: float = 0.0


def resample_chunk(chunk: np.ndarray, state: ResamplerState) -> np.ndarray:
    """Resample a single Float32 chunk to ``state.dst_rate``.

    The ``tail`` field in state carries the fractional source index
    across chunks so frame boundaries do not tick. Returns a Float32
    numpy array.
    """
    if state.src_rate == state.dst_rate:
        return chunk.astype(np.float32, copy=True)
    src_rate = state.src_rate
    dst_rate = state.dst_rate
    ratio = dst_rate / src_rate
    approx = int(np.floor(chunk.shape[0] * ratio + 1))
    out = np.empty(approx, dtype=np.float32)
    wi = 0
    pos = state.tail
    n = chunk.shape[0]
    step = src_rate / dst_rate
    while pos < n:
        i = int(np.floor(pos))
        frac = pos - i
        a = state.last_sample if i == 0 else chunk[i - 1]
        b = chunk[i]
        out[wi] = a + (b - a) * frac
        wi += 1
        pos += step
    state.tail = pos - n
    state.last_sample = float(chunk[-1])
    return out[:wi]


def resample_stream(
    chunks: List[np.ndarray], src_rate: int, dst_rate: int
) -> np.ndarray:
    state = ResamplerState(src_rate=src_rate, dst_rate=dst_rate)
    parts = [resample_chunk(np.asarray(c, dtype=np.float32), state) for c in chunks]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def float_to_int16(f32: np.ndarray) -> np.ndarray:
    """Match the JS int16 conversion exactly: asymmetric clamp at +1 to
    32767 (not 32768) so the full int16 range is used symmetrically for
    negatives and positives."""
    f = np.clip(f32, -1.0, 1.0)
    scaled = np.where(f < 0, f * 32768.0, f * 32767.0)
    return scaled.astype(np.int16)
