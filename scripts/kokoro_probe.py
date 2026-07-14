#!/usr/bin/env python3
"""Kokoro-82M de-risk probe: text -> misaki G2P -> 3 staged .tflite graphs
(+ host-side DSP) -> 24kHz wav.

Rough/exploratory by design (Task 2 of the TTS plan). See
Kokoro latency probe.
parameters this script encodes and how they were derived.

Usage:
    uv run python scripts/kokoro_probe.py "some text" runtime/probe_out.wav
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from ai_edge_litert.interpreter import Interpreter

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "runtime" / "models" / "kokoro-82m"

# ---- vocab (hexgrad/Kokoro-82M config.json "vocab") ----
VOCAB = {
    ';': 1, ':': 2, ',': 3, '.': 4, '!': 5, '?': 6, '—': 9, '…': 10, '"': 11,
    '(': 12, ')': 13, '“': 14, '”': 15, ' ': 16, '̃': 17,
    'ʣ': 18, 'ʥ': 19, 'ʦ': 20, 'ʨ': 21, 'ᵝ': 22, 'ꭧ': 23, 'A': 24, 'I': 25,
    'O': 31, 'Q': 33, 'S': 35, 'T': 36, 'W': 39, 'Y': 41, 'ᵊ': 42, 'a': 43,
    'b': 44, 'c': 45, 'd': 46, 'e': 47, 'f': 48, 'h': 50, 'i': 51, 'j': 52,
    'k': 53, 'l': 54, 'm': 55, 'n': 56, 'o': 57, 'p': 58, 'q': 59, 'r': 60,
    's': 61, 't': 62, 'u': 63, 'v': 64, 'w': 65, 'x': 66, 'y': 67, 'z': 68,
    'ɑ': 69, 'ɐ': 70, 'ɒ': 71, 'æ': 72, 'β': 75, 'ɔ': 76, 'ɕ': 77, 'ç': 78,
    'ɖ': 80, 'ð': 81, 'ʤ': 82, 'ə': 83, 'ɚ': 85, 'ɛ': 86, 'ɜ': 87, 'ɟ': 90,
    'ɡ': 92, 'ɥ': 99, 'ɨ': 101, 'ɪ': 102, 'ʝ': 103, 'ɯ': 110, 'ɰ': 111,
    'ŋ': 112, 'ɳ': 113, 'ɲ': 114, 'ɴ': 115, 'ø': 116, 'ɸ': 118, 'θ': 119,
    'œ': 120, 'ɹ': 123, 'ɾ': 125, 'ɻ': 126, 'ʁ': 128, 'ɽ': 129, 'ʂ': 130,
    'ʃ': 131, 'ʈ': 132, 'ʧ': 133, 'ʊ': 135, 'ʋ': 136, 'ʌ': 138, 'ɣ': 139,
    'ɤ': 140, 'χ': 142, 'ʎ': 143, 'ʒ': 147, 'ʔ': 148, 'ˈ': 156, 'ˌ': 157,
    'ː': 158, 'ʰ': 162, 'ʲ': 164, '↓': 169, '→': 171, '↗': 172, '↘': 173,
    'ᵻ': 177,
}

T_BUCKET = 128
L_BUCKET = 512
SR = 24000
N_FFT = 20
HOP = 5
WIN_LEN = 20
UPSAMPLE_SCALE = 300  # prod(upsample_rates=[10,6]) * gen_istft_hop_size(5)
SAMPLES_PER_FRAME = 600  # = 2 * UPSAMPLE_SCALE (F0 grid is 2x the asr/frame grid)
HARMONIC_NUM = 8
SINE_AMP = 0.1
NOISE_STD = 0.003
VOICED_THRESHOLD = 10.0


def g2p(text: str) -> str:
    from misaki import en

    if not hasattr(g2p, "_g2p"):
        g2p._g2p = en.G2P(trf=False, british=False, fallback=None)
    phonemes, _ = g2p._g2p(text)
    return phonemes


def load_graph(name: str) -> Interpreter:
    interp = Interpreter(model_path=str(MODEL_DIR / name))
    interp.allocate_tensors()
    return interp


def run_graph(interp: Interpreter, *inputs: np.ndarray) -> list[np.ndarray]:
    in_details = interp.get_input_details()
    out_details = interp.get_output_details()
    for detail, arr in zip(in_details, inputs):
        interp.set_tensor(detail["index"], arr)
    interp.invoke()
    return [interp.get_tensor(d["index"]) for d in out_details]


def hann_periodic(n: int) -> np.ndarray:
    # torch.hann_window(n, periodic=True)
    k = np.arange(n)
    return 0.5 - 0.5 * np.cos(2 * np.pi * k / n)


def linear_interp_half_pixel(x: np.ndarray, out_len: int) -> np.ndarray:
    """Replicates torch.nn.functional.interpolate(..., mode='linear',
    align_corners=False) along the last axis. x: [..., N] -> [..., out_len]."""
    in_len = x.shape[-1]
    scale = in_len / out_len
    out_idx = np.arange(out_len)
    src = (out_idx + 0.5) * scale - 0.5
    src = np.clip(src, 0, in_len - 1)
    lo = np.floor(src).astype(np.int64)
    hi = np.clip(lo + 1, 0, in_len - 1)
    frac = (src - lo)[None, :] if x.ndim > 1 else (src - lo)
    lo_v = np.take(x, lo, axis=-1)
    hi_v = np.take(x, hi, axis=-1)
    if x.ndim > 1:
        return lo_v + (hi_v - lo_v) * frac
    return lo_v + (hi_v - lo_v) * (src - lo)


def sine_gen(f0: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """f0: [T_full] fundamental frequency curve at full sample rate (already
    upsampled by UPSAMPLE_SCALE via nearest-neighbor). Returns merged
    harmonic-source waveform [T_full] (post l_linear + tanh)."""
    t_full = f0.shape[0]
    harmonics = np.arange(1, HARMONIC_NUM + 2, dtype=np.float32)  # 1..9
    fn = f0[:, None] * harmonics[None, :]  # [T_full, 9]
    rad_values = (fn / SR) % 1.0  # [T_full, 9]

    # downsample by 1/UPSAMPLE_SCALE (linear), cumsum, upsample back
    down_len = max(1, round(t_full / UPSAMPLE_SCALE))
    rad_t = rad_values.T  # [9, T_full]
    rad_down = linear_interp_half_pixel(rad_t, down_len)  # [9, down_len]
    phase_down = np.cumsum(rad_down, axis=1) * 2 * np.pi  # [9, down_len]
    phase_up = linear_interp_half_pixel(phase_down * UPSAMPLE_SCALE, t_full)  # [9, T_full]
    sines = np.sin(phase_up).T  # [T_full, 9]

    sine_waves = sines * SINE_AMP
    uv = (f0 > VOICED_THRESHOLD).astype(np.float32)  # [T_full]
    noise_amp = uv * NOISE_STD + (1 - uv) * SINE_AMP / 3
    noise = noise_amp[:, None] * rng.standard_normal((t_full, HARMONIC_NUM + 1)).astype(np.float32)
    sine_waves = sine_waves * uv[:, None] + noise

    params = np.load(MODEL_DIR / "host_dsp_params.npz")
    w = params["l_linear_weight"][0]  # [9]
    b = params["l_linear_bias"][0]
    merged = np.tanh(sine_waves @ w + b)  # [T_full]
    return merged.astype(np.float32)


def stft_forward(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """torch.stft(x, N_FFT, HOP, WIN_LEN, window=hann, center=True,
    pad_mode='reflect', onesided=True, return_complex=True) -> mag, phase.
    x: [T_full] -> mag, phase: [N_FFT//2+1, n_frames]"""
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad), mode="reflect")
    win = hann_periodic(WIN_LEN).astype(np.float32)
    n_frames = 1 + (len(xp) - N_FFT) // HOP
    shape = (n_frames, N_FFT)
    strides = (xp.strides[0] * HOP, xp.strides[0])
    frames = np.lib.stride_tricks.as_strided(xp, shape=shape, strides=strides) * win
    spec = np.fft.rfft(frames, n=N_FFT, axis=1).T  # [11, n_frames]
    return np.abs(spec).astype(np.float32), np.angle(spec).astype(np.float32)


def istft_overlap_add(mag: np.ndarray, phase: np.ndarray, Wr: np.ndarray, Wi: np.ndarray) -> np.ndarray:
    """mag, phase: [11, T]. Wr, Wi: [11, 20] staged bases (already include the
    1/N_FFT and window factor). Coefficients double all bins except DC (0)
    and Nyquist (10) to account for one-sided-spectrum conjugate symmetry —
    verified numerically to reproduce torch.istft to ~1e-7."""
    n_bins, T = mag.shape
    re = mag * np.cos(phase)
    im = mag * np.sin(phase)
    coef = np.full(n_bins, 2.0, dtype=np.float32)
    coef[0] = 1.0
    coef[-1] = 1.0
    re_c = re * coef[:, None]
    im_c = im * coef[:, None]
    frames = Wr.T @ re_c - Wi.T @ im_c  # [20, T]

    win = hann_periodic(WIN_LEN).astype(np.float32)
    out_len = (T - 1) * HOP + N_FFT
    out = np.zeros(out_len, dtype=np.float32)
    wsum = np.zeros(out_len, dtype=np.float32)
    win_sq = win ** 2
    for t in range(T):
        s = t * HOP
        out[s : s + N_FFT] += frames[:, t]
        wsum[s : s + N_FFT] += win_sq
    nz = wsum > 1e-11
    out[nz] /= wsum[nz]
    pad = N_FFT // 2
    return out[pad:-pad]


def synthesize(text: str, voice_name: str = "af_heart", seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)

    phonemes = g2p(text)
    ids = [VOCAB[c] for c in phonemes if c in VOCAB]
    n_phon = len(ids)
    input_ids = [0] + ids + [0]
    n = len(input_ids)
    if n > T_BUCKET:
        raise ValueError(f"phoneme sequence too long for bucket: {n} > {T_BUCKET}")

    # right-pad to T_BUCKET (real content at the START, matching the
    # reference model's text_mask convention: torch.arange(L)+1 > input_lengths
    # marks *trailing* positions as padding, so absolute position 0 must be
    # the first real token -- learned positional embeddings are position-
    # dependent, so left-padding here would shift every token's absolute
    # position and corrupt the duration/text encoder outputs.)
    ids_arr = np.zeros((1, T_BUCKET), dtype=np.int64)
    ids_arr[0, :n] = input_ids
    input_mask = np.zeros((1, T_BUCKET), dtype=np.float32)
    input_mask[0, :n] = 1.0

    voice_pack = np.load(MODEL_DIR / "voices" / f"{voice_name}.npy")  # [510,1,256]
    idx = min(max(n_phon - 1, 0), voice_pack.shape[0] - 1)
    ref_s = voice_pack[idx].reshape(1, 256).astype(np.float32)

    predictor = load_graph("kokoro_predictor.tflite")
    duration, d, t_en = run_graph(predictor, ids_arr, ref_s, input_mask)
    # duration: [1,128], d: [1,128,640], t_en: [1,512,128]

    pred_dur = np.round(duration[0]).clip(min=1).astype(np.int64)  # [128]
    frame_ptr = 0
    aln = np.zeros((1, T_BUCKET, L_BUCKET), dtype=np.float32)
    for tok_idx in range(0, n):
        dur = int(pred_dur[tok_idx])
        end = min(frame_ptr + dur, L_BUCKET)
        if end > frame_ptr:
            aln[0, tok_idx, frame_ptr:end] = 1.0
        frame_ptr = end
        if frame_ptr >= L_BUCKET:
            break
    total_frames = frame_ptr
    frame_mask = np.zeros((1, L_BUCKET), dtype=np.float32)
    frame_mask[0, :total_frames] = 1.0

    prosody = load_graph("kokoro_prosody.tflite")
    asr, F0, N = run_graph(prosody, d, t_en, aln, ref_s, frame_mask)
    # asr: [1,512,512], F0: [1,1024], N: [1,1024]

    f0_curve = F0[0]  # [1024]
    f0_full = np.repeat(f0_curve, UPSAMPLE_SCALE)  # nearest upsample -> [307200]
    har_source = sine_gen(f0_full, rng)  # [307200]
    har_spec, har_phase = stft_forward(har_source)  # [11, 61441] each
    har = np.concatenate([har_spec, har_phase], axis=0)[None].astype(np.float32)  # [1,22,61441]

    vocoder = load_graph("kokoro_vocoder.tflite")
    spec, phase = run_graph(vocoder, asr, F0, N, har, ref_s, frame_mask)
    # spec, phase: [1,11,61441]

    Wr = np.fromfile(MODEL_DIR / "istft_Wr_f32.bin", dtype=np.float32).reshape(11, 20)
    Wi = np.fromfile(MODEL_DIR / "istft_Wi_f32.bin", dtype=np.float32).reshape(11, 20)
    audio_full = istft_overlap_add(spec[0], phase[0], Wr, Wi)  # [307200]

    n_samples = total_frames * SAMPLES_PER_FRAME
    audio = audio_full[:n_samples]
    return audio.astype(np.float32)


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello world."
    out_path = sys.argv[2] if len(sys.argv) > 2 else str(ROOT / "runtime" / "probe_out.wav")
    audio = synthesize(text)
    sf.write(out_path, audio, SR)
    print(f"wrote {out_path}: {audio.shape[0]} samples ({audio.shape[0]/SR:.2f}s)")


if __name__ == "__main__":
    main()
