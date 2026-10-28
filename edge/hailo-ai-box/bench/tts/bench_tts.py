#!/usr/bin/env python3
"""CPU TTS bench for Raspberry Pi 5: pocket-tts, Phonon (ptts), Piper.

One process per (engine, model, voice, threads) so RSS and cold load are clean.
Writes a JSON result and WAVs of the first turn.

  python bench_tts.py --engine pocket --model english_18m_2026-10 --voice alba --threads 4
  python bench_tts.py --engine phonon --model default --voice default --lang en --threads 2
  python bench_tts.py --engine piper --model /path/en_US-amy-low.onnx --threads 4
"""
from __future__ import annotations

import argparse, gc, json, os, statistics, subprocess, threading, time, wave
from pathlib import Path

import numpy as np
import psutil

REPLIES = {
    "en": [
        "Okay, fan speed set to two.",
        "Okay, navigating to Rinconada Park. It is twelve minutes away, you arrive at three forty.",
        "Here is today's brief. The council approved the new tram line on Tuesday. Weather stays dry "
        "with highs near twenty degrees, and traffic on the ring road is light this morning, so expect no delays.",
    ],
    "de": [
        "Lüfter auf Stufe 2.",
        "Navigation zum Stadtpark gestartet, Ankunft in zwölf Minuten um fünfzehn Uhr vierzig.",
        "Hier die Nachrichten. Der Stadtrat hat am Dienstag die neue Straßenbahnlinie genehmigt, "
        "und das Wetter bleibt bis zum Wochenende trocken bei etwa zwanzig Grad.",
    ],
    "fr": [
        "Ventilateur réglé sur 2.",
        "Navigation vers le parc municipal, arrivée dans douze minutes à quinze heures quarante.",
        "Voici le résumé du jour. Le conseil municipal a approuvé mardi la nouvelle ligne de tramway, "
        "et le temps reste sec avec environ vingt degrés jusqu'à dimanche.",
    ],
}


def vc(*args):
    try:
        return subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return ""


def pi_state():
    t = vc("measure_temp")  # temp=63.7'C
    c = vc("measure_clock", "arm")  # frequency(48)=2400000000
    th = vc("get_throttled")
    try:
        temp = float(t.split("=")[1].split("'")[0])
    except Exception:
        temp = None
    try:
        mhz = int(c.split("=")[1]) / 1e6
    except Exception:
        mhz = None
    return {"temp_c": temp, "arm_mhz": mhz, "throttled": th.split("=")[-1] if th else None, "t": time.time()}


class Sampler(threading.Thread):
    """~2 Hz sampler of temp/clock/throttle/process cpu%/threads/rss."""

    def __init__(self, proc, period=0.5):
        super().__init__(daemon=True)
        self.proc, self.period, self.samples, self._stop_evt = proc, period, [], threading.Event()

    def run(self):
        self.proc.cpu_percent(None)
        while not self._stop_evt.is_set():
            s = pi_state()
            s["cpu_pct"] = self.proc.cpu_percent(None)
            s["threads"] = self.proc.num_threads()
            s["rss_mb"] = self.proc.memory_info().rss / 1e6
            s["sys_cpu_pct"] = psutil.cpu_percent(None)
            self.samples.append(s)
            self._stop_evt.wait(self.period)

    def stop(self):
        self._stop_evt.set()
        self.join()

    def window(self, t0, t1):
        return [s for s in self.samples if t0 <= s["t"] <= t1]


def summarize(ss, key):
    v = [s[key] for s in ss if s.get(key) is not None]
    return (min(v), max(v)) if v else (None, None)


def audio_metrics(x: np.ndarray, sr: int):
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return {"empty": True}
    a = np.abs(x)
    thr = 0.01
    nz = np.where(a > thr)[0]
    lead = (nz[0] / sr * 1000) if nz.size else None
    trail = ((x.size - 1 - nz[-1]) / sr * 1000) if nz.size else None
    # crude spectral rolloff: share of energy above 7 kHz (band-limit hint)
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    f = np.fft.rfftfreq(x.size, 1 / sr)
    hf = float(spec[f > 7000].sum() / max(spec.sum(), 1e-12))
    # sample-to-sample jumps (click detector)
    d = np.abs(np.diff(x))
    return {
        "sr": sr, "dur_s": round(x.size / sr, 3), "peak": round(float(a.max()), 3),
        "rms_dbfs": round(20 * np.log10(float(np.sqrt((x ** 2).mean())) + 1e-9), 1),
        "clipped_frac": round(float((a >= 0.99).mean()), 6), "dc": round(float(x.mean()), 5),
        "lead_sil_ms": None if lead is None else round(lead), "trail_sil_ms": None if trail is None else round(trail),
        "hf_energy_gt7k": round(hf, 5), "max_step": round(float(d.max()), 3),
        "n_big_steps_gt0.5": int((d > 0.5).sum()),
    }


def write_wav(path, x, sr):
    x = np.clip(np.asarray(x, dtype=np.float32), -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())


# ---------------------------------------------------------------- engines
class Pocket:
    def __init__(self, model, voice, threads, args):
        import torch
        torch.set_num_threads(threads)
        from pocket_tts import TTSModel
        self.torch = torch
        self.m = TTSModel.load_model(language=model, quantize=args.quantize)
        self.sr = int(self.m.sample_rate)
        t = time.perf_counter()
        self.state = self.m.get_state_for_audio_prompt(voice)
        self.voice_prep_s = time.perf_counter() - t

    def stream(self, text):
        with self.torch.no_grad():
            for ch in self.m.generate_audio_stream(self.state, text):
                yield ch.detach().cpu().numpy().reshape(-1)


class Phonon:
    def __init__(self, model, voice, threads, args):
        import ptts
        ptts.set_num_threads(threads)
        kw = {"lang": args.lang}
        if args.quant:
            kw["quant"] = args.quant
        self.t = ptts.TTS(None if model == "default" else model, **kw)
        self.voice = None if voice == "default" else voice
        self.sr = int(self.t.sample_rate)
        t = time.perf_counter()
        self.t.synth("Hi.", voice=self.voice)  # resolves/downloads the voice
        self.voice_prep_s = time.perf_counter() - t

    def stream(self, text):
        with self.t.stream(text, voice=self.voice) as a:
            for ch in a:
                yield np.asarray(ch, dtype=np.float32).reshape(-1)


class Piper:
    def __init__(self, model, voice, threads, args):
        import onnxruntime as ort
        from piper import PiperVoice
        v = PiperVoice.load(model)
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        v.session = ort.InferenceSession(str(model), sess_options=so, providers=["CPUExecutionProvider"])
        self.v = v
        self.sr = int(v.config.sample_rate)
        self.voice_prep_s = 0.0

    def stream(self, text):
        for ch in self.v.synthesize(text):
            yield np.asarray(ch.audio_float_array, dtype=np.float32).reshape(-1)


ENGINES = {"pocket": Pocket, "phonon": Phonon, "piper": Piper}


def wait_for_load(limit, max_wait):
    waited = 0
    while os.getloadavg()[0] > limit and waited < max_wait:
        time.sleep(15); waited += 15
    return waited


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=ENGINES)
    ap.add_argument("--model", default="default", help="pocket language cfg / phonon hf:// repo or 'default' / piper .onnx path")
    ap.add_argument("--voice", default="default")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--quant", default=None, help="phonon quant e.g. q8_0")
    ap.add_argument("--quantize", action="store_true", help="pocket int8 dynamic quant")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--load-limit", type=float, default=2.0)
    ap.add_argument("--max-wait", type=int, default=240)
    ap.add_argument("--out", default="results")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    out = Path(args.out); (out / "wav").mkdir(parents=True, exist_ok=True)
    name = f"{args.engine}_{Path(args.model).stem if args.engine=='piper' else args.model.replace('/','_').replace(':','_')}_{args.voice}_{args.lang}_t{args.threads}{args.tag}"
    waited = wait_for_load(args.load_limit, args.max_wait)
    proc = psutil.Process()
    res = {"name": name, "args": vars(args), "load1_at_start": os.getloadavg()[0], "waited_for_load_s": waited,
           "pi_start": pi_state(), "ncpu": os.cpu_count()}
    smp = Sampler(proc); smp.start()
    Eng = ENGINES[args.engine]

    t = time.perf_counter(); eng = Eng(args.model, args.voice, args.threads, args)
    res["cold_load_s"] = round(time.perf_counter() - t, 3)  # first load in a fresh process (files already downloaded/page-cached)
    res["voice_prep_s"] = round(eng.voice_prep_s, 3)
    res["rss_after_load_mb"] = round(proc.memory_info().rss / 1e6, 1)
    del eng; gc.collect()
    t = time.perf_counter(); eng = Eng(args.model, args.voice, args.threads, args)
    res["warm_load_s"] = round(time.perf_counter() - t, 3)  # second load in same process
    res["rss_after_warm_load_mb"] = round(proc.memory_info().rss / 1e6, 1)
    res["sample_rate"] = eng.sr

    def run(text, keep=False):
        t0 = time.perf_counter(); wall0 = time.time(); ttfa = None; chunks = []; n = 0
        for ch in eng.stream(text):
            if ttfa is None:
                ttfa = time.perf_counter() - t0
            n += 1
            chunks.append(ch)
        total = time.perf_counter() - t0
        pcm = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        return {"ttfa_ms": None if ttfa is None else ttfa * 1000, "total_s": total, "audio_s": pcm.size / eng.sr,
                "rtf": total / max(pcm.size / eng.sr, 1e-9), "chunks": n, "wall0": wall0, "wall1": time.time()}, pcm

    warm, _ = run("Okay, one moment.")
    res["first_call"] = {k: round(v, 3) for k, v in warm.items() if k in ("ttfa_ms", "total_s", "audio_s", "rtf")}

    texts = REPLIES.get(args.lang, REPLIES["en"])
    turns, c0 = [], proc.cpu_times()
    s_run0 = time.time(); cpu_t0 = time.process_time()
    for rep in range(args.reps):
        rows, turn_t0 = [], time.perf_counter()
        for i, text in enumerate(texts):
            cp = proc.cpu_times(); tw = time.perf_counter()
            r, pcm = run(text)
            wall = time.perf_counter() - tw; cp2 = proc.cpu_times()
            cpu_pct = 100 * ((cp2.user + cp2.system) - (cp.user + cp.system)) / max(wall, 1e-9)
            w = smp.window(r["wall0"], r["wall1"])
            r.update({"words": len(text.split()), "cpu_pct": round(cpu_pct, 1),
                      "temp_c": summarize(w, "temp_c"), "arm_mhz": summarize(w, "arm_mhz"),
                      "threads_seen": summarize(w, "threads"), "n_samples": len(w),
                      "throttled_seen": sorted({s["throttled"] for s in w if s["throttled"]})})
            if rep == 0:
                r["audio"] = audio_metrics(pcm, eng.sr)
                write_wav(out / "wav" / f"{name}_r{i}.wav", pcm, eng.sr)
            for k in ("ttfa_ms", "total_s", "audio_s", "rtf"):
                r[k] = round(r[k], 3)
            r.pop("wall0"); r.pop("wall1")
            rows.append(r)
        turns.append({"replies": rows, "turn_total_s": round(time.perf_counter() - turn_t0, 3)})
    s_run1 = time.time()
    allw = smp.window(s_run0, s_run1)
    res["turns"] = turns
    res["agg"] = {}
    for i in range(len(texts)):
        tt = [t["replies"][i]["ttfa_ms"] for t in turns]
        tot = [t["replies"][i]["total_s"] for t in turns]
        rtf = [t["replies"][i]["rtf"] for t in turns]
        cpu = [t["replies"][i]["cpu_pct"] for t in turns]
        res["agg"][f"reply{i}_{len(texts[i].split())}w"] = {
            "ttfa_ms_p50": round(statistics.median(tt), 1), "ttfa_ms_max": round(max(tt), 1),
            "total_s_p50": round(statistics.median(tot), 3), "rtf_p50": round(statistics.median(rtf), 3),
            "cpu_pct_p50": round(statistics.median(cpu), 1)}
    res["agg"]["turn_total_s_p50"] = round(statistics.median(t["turn_total_s"] for t in turns), 3)
    res["agg"]["cpu_pct_overall"] = round(100 * (time.process_time() - cpu_t0) / (s_run1 - s_run0), 1)
    res["agg"]["cpu_pct_sampler_max"] = summarize(allw, "cpu_pct")[1]
    res["agg"]["threads_seen"] = summarize(allw, "threads")
    res["agg"]["temp_c_range"] = summarize(allw, "temp_c")
    res["agg"]["arm_mhz_range"] = summarize(allw, "arm_mhz")
    res["agg"]["throttled_seen"] = sorted({s["throttled"] for s in allw if s["throttled"]})
    res["agg"]["sys_cpu_pct_max"] = summarize(allw, "sys_cpu_pct")[1]
    res["rss_peak_mb"] = round(max(s["rss_mb"] for s in smp.samples), 1)
    smp.stop()
    res["pi_end"] = pi_state(); res["load1_at_end"] = os.getloadavg()[0]
    res["samples"] = smp.samples
    (out / f"{name}.json").write_text(json.dumps(res, indent=1))
    a = res["agg"]
    print(f"{name}: cold {res['cold_load_s']}s warm {res['warm_load_s']}s rss {res['rss_after_warm_load_mb']}MB peak {res['rss_peak_mb']} "
          f"| " + " ".join(f"{k}:ttfa{v['ttfa_ms_p50']}ms rtf{v['rtf_p50']}" for k, v in a.items() if isinstance(v, dict) and 'rtf_p50' in v)
          + f" | cpu {a['cpu_pct_overall']}% temp {a['temp_c_range']} clk {a['arm_mhz_range']} thr {a['throttled_seen']} load1 {res['load1_at_start']:.2f}")


if __name__ == "__main__":
    main()
