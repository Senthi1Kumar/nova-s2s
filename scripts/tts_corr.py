#!/usr/bin/env python3
"""Magnitude-spectrogram Pearson correlation between two wavs (probe vs golden)."""
import sys
import numpy as np
import soundfile as sf


def mag_spec(x, n_fft=1024, hop=256):
    win = np.hanning(n_fft).astype(np.float32)
    n_frames = 1 + (len(x) - n_fft) // hop
    if n_frames <= 0:
        pad = n_fft - len(x)
        x = np.pad(x, (0, pad))
        n_frames = 1
    shape = (n_frames, n_fft)
    strides = (x.strides[0] * hop, x.strides[0])
    frames = np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides) * win
    spec = np.abs(np.fft.rfft(frames, axis=1))
    return spec


def main():
    a_path, b_path = sys.argv[1], sys.argv[2]
    a, sr_a = sf.read(a_path, dtype="float32")
    b, sr_b = sf.read(b_path, dtype="float32")
    assert sr_a == sr_b, (sr_a, sr_b)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    sa = mag_spec(a)
    sb = mag_spec(b)
    m = min(sa.shape[0], sb.shape[0])
    sa, sb = sa[:m].flatten(), sb[:m].flatten()
    corr = np.corrcoef(sa, sb)[0, 1]
    print(f"{a_path} len={len(a)} vs {b_path} len={len(b)}  (n={n} used)")
    print(f"magnitude-spectrogram correlation: {corr:.6f}")


if __name__ == "__main__":
    main()
