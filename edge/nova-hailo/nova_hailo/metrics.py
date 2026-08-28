from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _pct(values, p: float) -> float | None:
    """Nearest-rank percentile. Small n is normal here, so no interpolation."""
    vals = sorted(values)
    if not vals:
        return None
    return vals[min(int(len(vals) * p), len(vals) - 1)]


def _r(v: float | None, nd: int = 2) -> float | None:
    if v is None:
        return None
    return round(v, nd)


@dataclass
class InferenceMetrics:
    """Per LLM call — host-side GenAI profiling (nolow-level Hailo monitor on 10H/5.1.1)."""

    prompt_tokens_est: int = 0
    generated_tokens: int = 0
    prefill_start: float | None = None
    first_token_time: float | None = None
    decode_start: float | None = None
    decode_end: float | None = None
    total_start: float | None = None
    total_end: float | None = None
    token_timestamps: list = field(default_factory=list)
    device: str = "hailo10h_npu"
    no_think: bool = False

    def start(self, prompt_tokens_est: int = 0):
        self.prompt_tokens_est = prompt_tokens_est
        now = time.perf_counter()
        self.total_start = now
        self.prefill_start = now

    def record_token(self, idx: int):
        now = time.perf_counter()
        self.token_timestamps.append(now)
        if idx == 0:
            self.first_token_time = now
            self.decode_start = now
        self.generated_tokens = idx + 1

    def end(self):
        now = time.perf_counter()
        self.decode_end = now
        self.total_end = now

    @property
    def ttft_ms(self) -> float | None:
        if self.prefill_start is None or self.first_token_time is None:
            return None
        return (self.first_token_time - self.prefill_start) * 1000

    @property
    def prefill_ms(self) -> float | None:
        return self.ttft_ms

    @property
    def decode_ms(self) -> float | None:
        if self.decode_start is None or self.decode_end is None:
            return None
        return (self.decode_end - self.decode_start) * 1000

    @property
    def total_ms(self) -> float | None:
        if self.total_start is None or self.total_end is None:
            return None
        return (self.total_end - self.total_start) * 1000

    @property
    def tok_s(self) -> float | None:
        if not self.generated_tokens or not self.total_ms or self.total_ms <= 0:
            return None
        return self.generated_tokens / (self.total_ms / 1000.0)

    @property
    def decode_tok_s(self) -> float | None:
        if not self.generated_tokens or not self.decode_ms or self.decode_ms <= 0:
            return None
        return self.generated_tokens / (self.decode_ms / 1000.0)

    @property
    def ms_per_token(self) -> float | None:
        tps = self.decode_tok_s
        if not tps:
            return None
        return 1000.0 / tps

    @property
    def interarrival_ms(self) -> list[float]:
        if len(self.token_timestamps) < 2:
            return []
        return [
            (self.token_timestamps[i] - self.token_timestamps[i - 1]) * 1000
            for i in range(1, len(self.token_timestamps))
        ]

    def _pct(self, p: float) -> float | None:
        vals = sorted(self.interarrival_ms)
        if not vals:
            return None
        idx = min(int(len(vals) * p), len(vals) - 1)
        return vals[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "no_think": self.no_think,
            "prompt_tokens_est": self.prompt_tokens_est,
            "generated_tokens": self.generated_tokens,
            "ttft_ms": _r(self.ttft_ms),
            "prefill_ms": _r(self.prefill_ms),
            "decode_ms": _r(self.decode_ms),
            "llm_total_ms": _r(self.total_ms),
            "tok_s": _r(self.tok_s),
            "decode_tok_s": _r(self.decode_tok_s),
            "ms_per_token": _r(self.ms_per_token),
            "token_latency_p50_ms": _r(self._pct(0.50)),
            "token_latency_p95_ms": _r(self._pct(0.95)),
            "token_latency_p99_ms": _r(self._pct(0.99)),
        }


@dataclass
class TurnMetrics:
    """
    Cascaded turn metrics with device labels.

    ASR/LLM = hailo10h_npu host-timed streaming stats.
    TTS = cpu_onnx (Piper synth + optional play).
    VAD = cpu.
    """

    stt_ms: float | None = None
    stt_load_ms: float | None = None
    stt_infer_ms: float | None = None
    auth_ms: float | None = None
    llm_ms: float | None = None
    tool_ms: float | None = None
    tts_ms: float | None = None
    tts_synth_ms: float | None = None
    tts_play_ms: float | None = None
    ttfb_ms: float | None = None
    ttfa_ms: float | None = None  # wall to first audio sample
    # speech_stopped → sound actually leaving the speaker, taken from the
    # browser playback.started ack. This is the user-perceived TTFA and the
    # number the release gate is written against.
    speech_end_to_audible_ms: float | None = None
    # speech_stopped → first PCM chunk handed to the transport. Server-side
    # only: excludes network and browser buffering, so it always flatters the
    # real figure. Kept because the gap between the two IS playback_ack_ms.
    speech_end_to_first_pcm_ms: float | None = None
    # "playback_ack" (measured) | "first_pcm" (fallback, no browser ack)
    audible_source: str | None = None
    playback_ack_ms: float | None = None  # browser first-audio ACK lag
    overlap_ms: float | None = None  # LLM decode overlapping TTS
    total_latency_ms: float | None = None
    barge_in: bool = False
    barge_in_at_ms: float | None = None
    barge_in_stop_ms: float | None = None  # interrupt → playback stop ACK
    generation_id: int | None = None
    auth_decision: str | None = None
    tool_name: str | None = None
    llm: dict | None = None
    llm_calls: list = field(default_factory=list)
    devices: dict = field(
        default_factory=lambda: {
            "vad": "cpu",
            "asr": "hailo10h_npu",
            "llm": "hailo10h_npu",
            "tts": "cpu_onnx_piper",
            "auth": "cpu",
        }
    )
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "devices": self.devices,
            "stt_ms": _r(self.stt_ms, 1),
            "stt_load_ms": _r(self.stt_load_ms, 1),
            "stt_infer_ms": _r(self.stt_infer_ms, 1),
            "auth_ms": _r(self.auth_ms, 1),
            "llm_ms": _r(self.llm_ms, 1),
            "llm_ttft_ms": _r(
                (self.llm or {}).get("ttft_ms") if isinstance(self.llm, dict) else None
            ),
            "llm_decode_ms": _r(
                (self.llm or {}).get("decode_ms") if isinstance(self.llm, dict) else None
            ),
            "llm_total_ms": _r(
                (self.llm or {}).get("llm_total_ms") if isinstance(self.llm, dict) else None
            ),
            "tool_ms": _r(self.tool_ms, 1),
            "tts_ms": _r(self.tts_ms, 1),
            "tts_synth_ms": _r(self.tts_synth_ms, 1),
            "tts_play_ms": _r(self.tts_play_ms, 1),
            "ttfb_ms": _r(self.ttfb_ms, 1),
            "ttfa_ms": _r(self.ttfa_ms, 1),
            "speech_end_to_audible_ms": _r(self.speech_end_to_audible_ms, 1),
            "speech_end_to_first_pcm_ms": _r(self.speech_end_to_first_pcm_ms, 1),
            "audible_source": self.audible_source,
            "playback_ack_ms": _r(self.playback_ack_ms, 1),
            "overlap_ms": _r(self.overlap_ms, 1),
            "total_latency_ms": _r(self.total_latency_ms, 1),
            "barge_in": self.barge_in,
            "barge_in_at_ms": _r(self.barge_in_at_ms, 1),
            "barge_in_stop_ms": _r(self.barge_in_stop_ms, 1),
            "generation_id": self.generation_id,
            "auth_decision": self.auth_decision,
            "tool_name": self.tool_name,
        }
        if self.llm:
            d["llm"] = self.llm
            for k in (
                "ttft_ms",
                "prefill_ms",
                "decode_ms",
                "tok_s",
                "decode_tok_s",
                "ms_per_token",
                "generated_tokens",
                "token_latency_p50_ms",
                "token_latency_p95_ms",
                "no_think",
                "device",
            ):
                if k in self.llm and self.llm[k] is not None:
                    d[k] = self.llm[k]
            if d.get("llm_ms") is None and self.llm.get("llm_total_ms") is not None:
                d["llm_ms"] = _r(self.llm.get("llm_total_ms"))
            if d.get("llm_ttft_ms") is None and self.llm.get("ttft_ms") is not None:
                d["llm_ttft_ms"] = _r(self.llm.get("ttft_ms"))
            if d.get("llm_decode_ms") is None and self.llm.get("decode_ms") is not None:
                d["llm_decode_ms"] = _r(self.llm.get("decode_ms"))
        if self.llm_calls:
            d["llm_calls"] = self.llm_calls
        if self.notes:
            d["notes"] = self.notes
        return d

    def pretty(self) -> str:
        d = self.to_dict()
        lines = [
            "── turn metrics ──",
            f"  devices       : asr={self.devices.get('asr')}  llm={self.devices.get('llm')}  tts={self.devices.get('tts')}",
            f"  total_latency : {d.get('total_latency_ms')} ms",
            f"  ttfb          : {d.get('ttfb_ms')} ms  (first LLM token, wall)",
            f"  ttfa          : {d.get('ttfa_ms')} ms  (first audio)",
            f"  overlap       : {d.get('overlap_ms')} ms  (LLM∥TTS)",
            f"  stt [NPU]     : {d.get('stt_ms')} ms  (load {d.get('stt_load_ms')} + infer {d.get('stt_infer_ms')})",
            f"  auth [CPU]    : {d.get('auth_ms')} ms  ({d.get('auth_decision')})",
            f"  llm [NPU]     : {d.get('llm_ms')} ms",
            f"  tool [CPU]    : {d.get('tool_ms')} ms  ({d.get('tool_name')})",
            f"  tts [CPU]     : {d.get('tts_ms')} ms  (synth {d.get('tts_synth_ms')} + play {d.get('tts_play_ms')})",
        ]
        if d.get("ttft_ms") is not None:
            lines += [
                f"  ttft/prefill  : {d.get('ttft_ms')} ms",
                f"  decode        : {d.get('decode_ms')} ms",
                f"  tok/s         : {d.get('tok_s')}  (decode {d.get('decode_tok_s')})",
                f"  ms/tok        : {d.get('ms_per_token')}",
                f"  tokens        : {d.get('generated_tokens')}  no_think={d.get('no_think')}",
                f"  tok p50/p95   : {d.get('token_latency_p50_ms')} / {d.get('token_latency_p95_ms')} ms",
            ]
        for n in self.notes:
            lines.append(f"  note          : {n}")
        return "\n".join(lines)


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000.0


class SessionMetrics:
    def __init__(self):
        self.turns: list[TurnMetrics] = []
        self._ttfb = deque(maxlen=50)
        self._total = deque(maxlen=50)
        self._tok_s = deque(maxlen=50)
        self._stt = deque(maxlen=50)
        self._tts = deque(maxlen=50)
    def _audible_split(self) -> tuple[list[float], int]:
        """Pull TTFA from the stored turns at read time, not at add() time.

        The pipeline calls add() when the turn's own work finishes, but
        speech_end_to_audible_ms is filled in later by the realtime session
        once the browser playback ack lands. Snapshotting on add() therefore
        always saw None. TurnMetrics objects are retained, so read them late.

        Only turns with a real playback ack are pooled; ones that fell back to
        the send timestamp are counted separately, because averaging measured
        and optimistic values together quietly understates the gate figure.
        """
        measured: list[float] = []
        fallback = 0
        for t in self.turns:
            if t.speech_end_to_audible_ms is None:
                continue
            if t.audible_source == "playback_ack":
                measured.append(t.speech_end_to_audible_ms)
            else:
                fallback += 1
        return measured[-50:], fallback

    def add(self, turn: TurnMetrics):
        self.turns.append(turn)
        if turn.ttfb_ms is not None:
            self._ttfb.append(turn.ttfb_ms)
        if turn.total_latency_ms is not None:
            self._total.append(turn.total_latency_ms)
        if turn.llm and turn.llm.get("decode_tok_s"):
            self._tok_s.append(turn.llm["decode_tok_s"])
        if turn.stt_ms is not None:
            self._stt.append(turn.stt_ms)
        if turn.tts_ms is not None:
            self._tts.append(turn.tts_ms)

    def summary(self) -> dict:
        def avg(xs):
            return round(sum(xs) / len(xs), 2) if xs else None

        return {
            "turns": len(self.turns),
            "avg_ttfb_ms": avg(self._ttfb),
            "avg_total_latency_ms": avg(self._total),
            "avg_decode_tok_s": avg(self._tok_s),
            "avg_stt_ms": avg(self._stt),
            "avg_tts_ms": avg(self._tts),
            # Audible TTFA: the release-gate number. p50/p95 over measured
            # turns only; ttfa_unmeasured_turns says how much was excluded, so
            # a small sample can never look authoritative by accident.
            **self._ttfa_block(),
        }

    def _ttfa_block(self) -> dict:
        measured, fallback = self._audible_split()
        return {
            "ttfa_p50_ms": _r(_pct(measured, 0.50), 1),
            "ttfa_p95_ms": _r(_pct(measured, 0.95), 1),
            "ttfa_min_ms": _r(min(measured), 1) if measured else None,
            "ttfa_max_ms": _r(max(measured), 1) if measured else None,
            "ttfa_measured_turns": len(measured),
            "ttfa_unmeasured_turns": fallback,
        }
