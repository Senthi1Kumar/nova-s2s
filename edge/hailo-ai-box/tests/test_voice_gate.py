import asyncio
import dataclasses
import io
import json
import shutil
import subprocess
import time
import wave

import numpy as np
import pytest
from websockets.sync.client import connect

from aibox.config import load_settings
from aibox.voice import pipecat_services
from aibox.voice.audio import to_16k, wav_to_pcm16
from tests.test_api_inference import live  # noqa: F401

WINDOW = 512


class StubVad:
    """Scores consecutive windows from a per-window confidence list (0.0 past its end)."""

    def __init__(self, confidences):
        self.confidences = list(confidences)
        self.calls = 0

    def voice_confidence(self, buffer):
        assert len(buffer) == WINDOW * 2
        i, self.calls = self.calls, self.calls + 1
        return self.confidences[i] if i < len(self.confidences) else 0.0


class RecordingAsr:
    def __init__(self, text="hello box"):
        self.text = text
        self.lengths = []

    async def transcribe(self, pcm16k, language=None):
        self.lengths.append(len(pcm16k))
        return self.text


class ExplodingChat:
    calls = 0

    async def stream(self, model, messages, max_tokens, images=None):
        ExplodingChat.calls += 1
        yield "ok."


@pytest.fixture
def gate(services, monkeypatch):
    def configure(confidences, **overrides):
        stub = StubVad(confidences)
        monkeypatch.setattr(pipecat_services, "_create_vad", lambda: stub)
        services.settings = dataclasses.replace(services.settings, voice_vad=True, **overrides)
        services.asr = RecordingAsr()
        return stub

    return configure


def _turn(port, windows, frame_windows=2, wait_partial=0.0, terminal=("ignored", "done", "error")):
    pcm = (np.ones(windows * WINDOW) * 1000).astype("<i2").tobytes()
    step = frame_windows * WINDOW * 2
    events = []
    with connect(f"ws://127.0.0.1:{port}/v1/voice/stream") as ws:
        ws.send(json.dumps({"type": "start", "protocol": 2, "model": "qwen3-vl"}))
        for off in range(0, len(pcm), step):
            ws.send(pcm[off:off + step])
        if wait_partial:
            time.sleep(wait_partial)
        ws.send(json.dumps({"type": "stop"}))
        deadline = time.time() + 10
        while time.time() < deadline:
            raw = ws.recv(timeout=10)
            if isinstance(raw, bytes):
                continue
            event = json.loads(raw)
            events.append(event)
            if event["type"] in terminal:
                break
    return events


def _ignored(events):
    return next(e for e in events if e["type"] == "ignored")


def test_non_speech_is_ignored_without_asr(live, services, gate):  # noqa: F811
    gate([])
    events = _turn(live, 40)
    ignored = _ignored(events)
    assert ignored["reason"] == "no_speech"
    assert ignored["speech_ms"] == 0
    assert services.asr.lengths == []
    assert not any(e["type"] in {"transcript", "token"} for e in events)


def test_short_speech_is_ignored_as_too_short(live, services, gate):  # noqa: F811
    gate([0.9] * 5, voice_partials=False)
    ignored = _ignored(_turn(live, 40))
    assert ignored["reason"] == "too_short"
    assert ignored["speech_ms"] == 160
    assert services.asr.lengths == []


def test_low_speech_ratio_is_ignored(live, services, gate):  # noqa: F811
    gate([0.9] * 12, voice_partials=False)
    ignored = _ignored(_turn(live, 120))
    assert ignored["reason"] == "too_short"
    assert ignored["speech_ms"] == 384
    assert services.asr.lengths == []


def test_speech_is_trimmed_around_first_and_last_window(live, services, gate):  # noqa: F811
    gate([0.0] * 10 + [0.9] * 10, voice_partials=False)
    events = _turn(live, 40, terminal=("done", "ignored", "error"))
    assert events[-1]["type"] == "done"
    pad = 300 * 16
    start = max(0, 10 * WINDOW - pad)
    end = min(40 * WINDOW, 20 * WINDOW + pad)
    assert services.asr.lengths[-1] == end - start


def test_lone_filler_on_short_speech_is_ignored_as_hallucination(live, services, gate):  # noqa: F811
    gate([0.9] * 12, voice_partials=False)
    services.asr = RecordingAsr("Thank you.")
    ExplodingChat.calls = 0
    services.chat = ExplodingChat()
    ignored = _ignored(_turn(live, 24))
    assert ignored["reason"] == "hallucination"
    assert services.asr.lengths
    assert ExplodingChat.calls == 0


def test_filler_after_long_speech_is_kept(live, services, gate):  # noqa: F811
    gate([0.9] * 30)
    services.asr = RecordingAsr("Thank you.")
    events = _turn(live, 40)
    assert any(e["type"] == "transcript" for e in events)


def test_partials_are_throttled(live, services, gate):  # noqa: F811
    gate([0.9] * 200)
    events = _turn(live, 200, frame_windows=1)
    partials = [e for e in events if e["type"] == "transcript_partial"]
    assert len(services.asr.lengths) <= 3
    assert len(partials) <= 2


def test_partials_can_be_disabled(live, services, gate):  # noqa: F811
    gate([0.9] * 60, voice_partials=False)
    events = _turn(live, 60, frame_windows=1, wait_partial=1.0)
    assert not any(e["type"] == "transcript_partial" for e in events)
    assert len(services.asr.lengths) == 1


def test_real_silero_ignores_silence_and_noise(live, services):  # noqa: F811
    try:
        pipecat_services._create_vad()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Silero VAD unavailable: {exc}")
    services.settings = dataclasses.replace(services.settings, voice_vad=True)
    services.asr = RecordingAsr()
    rng = np.random.default_rng(0)
    for pcm in (
        np.zeros(16000, "<i2"),
        (rng.uniform(-1, 1, 16000) * 8000).astype("<i2"),
    ):
        with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
            ws.send(json.dumps({"type": "start", "protocol": 2}))
            raw = pcm.tobytes()
            for off in range(0, len(raw), 4096):
                ws.send(raw[off:off + 4096])
            ws.send(json.dumps({"type": "stop"}))
            events = []
            while True:
                msg = ws.recv(timeout=10)
                if isinstance(msg, bytes):
                    continue
                events.append(json.loads(msg))
                if events[-1]["type"] in {"ignored", "done", "error"}:
                    break
        assert events[-1]["type"] == "ignored"
    assert services.asr.lengths == []


def test_create_vad_returns_a_usable_analyzer():
    try:
        vad = pipecat_services._create_vad()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Silero VAD unavailable: {exc}")
    assert vad.sample_rate == 16000
    noise = (np.random.default_rng(1).uniform(-1, 1, WINDOW) * 8000).astype("<i2").tobytes()
    assert not isinstance(vad.voice_confidence(noise), int)


def test_vad_failure_falls_back_to_ungated_asr(live, services, monkeypatch):  # noqa: F811
    class BrokenVad:
        def voice_confidence(self, buffer):
            return 0  # pipecat's error sentinel

    monkeypatch.setattr(pipecat_services, "_create_vad", BrokenVad)
    services.settings = dataclasses.replace(services.settings, voice_vad=True)
    services.asr = RecordingAsr()
    events = _turn(live, 40)
    assert events[-1]["type"] == "done"
    assert services.asr.lengths[-1] == 40 * WINDOW


SPEECH_TEXT = "Turn on the cabin lights and set the temperature to twenty one degrees."


def _synth_speech_16k(voice_path: str) -> bytes:
    """Piper if its voice is present, else espeak-ng; skips when neither is available."""
    wav = None
    try:
        from piper import PiperVoice

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            PiperVoice.load(voice_path).synthesize_wav(SPEECH_TEXT, w)
        wav = buf.getvalue()
    except Exception:  # noqa: BLE001
        if shutil.which("espeak-ng"):
            wav = subprocess.run(
                ["espeak-ng", "--stdout", SPEECH_TEXT], capture_output=True, check=True
            ).stdout
    if wav is None:
        pytest.skip("no speech synthesizer available (piper voice or espeak-ng)")
    pcm, rate = wav_to_pcm16(wav)
    return asyncio.run(to_16k(pcm, rate))


def test_real_silero_detects_synthesized_speech(live, services):  # noqa: F811
    pcm = _synth_speech_16k(services.settings.piper_voice)
    services.settings = dataclasses.replace(services.settings, voice_vad=True, voice_partials=False)
    services.asr = RecordingAsr()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send(json.dumps({"type": "start", "protocol": 2}))
        for off in range(0, len(pcm), 4096):
            ws.send(pcm[off:off + 4096])
        ws.send(json.dumps({"type": "stop"}))
        events = []
        while True:
            msg = ws.recv(timeout=10)
            if isinstance(msg, bytes):
                continue
            events.append(json.loads(msg))
            if events[-1]["type"] in {"ignored", "transcript", "error"}:
                break
    assert events[-1]["type"] == "transcript", events[-1]
    assert services.asr.lengths


def test_vad_settings_env_parsing():
    s = load_settings({})
    assert (s.voice_vad, s.voice_vad_threshold, s.voice_min_speech_ms) == (True, 0.5, 300)
    assert (s.voice_min_speech_ratio, s.voice_partials) == (0.15, True)
    s = load_settings({
        "AIBOX_VOICE_VAD": "0", "AIBOX_VOICE_VAD_THRESHOLD": "0.7",
        "AIBOX_VOICE_MIN_SPEECH_MS": "500", "AIBOX_VOICE_MIN_SPEECH_RATIO": "0.3",
        "AIBOX_VOICE_PARTIALS": "off",
    })
    assert (s.voice_vad, s.voice_vad_threshold, s.voice_min_speech_ms) == (False, 0.7, 500)
    assert (s.voice_min_speech_ratio, s.voice_partials) == (0.3, False)


def test_events_report_the_vad_state_on(live, services, gate):  # noqa: F811
    gate([0.9] * 40)
    events = _turn(live, 40, terminal=("done",))
    assert next(e for e in events if e["type"] == "session")["vad"] == "on"
    assert next(e for e in events if e["type"] == "transcript")["vad"] == "on"
    assert events[-1]["vad"] == "on"


def test_ignored_reports_the_vad_state(live, services, gate):  # noqa: F811
    gate([])
    assert _ignored(_turn(live, 40))["vad"] == "on"


def test_events_report_the_vad_state_off(live, services):  # noqa: F811
    events = _turn(live, 40, terminal=("done",))
    assert next(e for e in events if e["type"] == "session")["vad"] == "off"
    assert events[-1]["vad"] == "off"


def test_events_report_the_vad_state_failed(live, services, monkeypatch):  # noqa: F811
    class BrokenVad:
        def voice_confidence(self, buffer):
            return 0

    monkeypatch.setattr(pipecat_services, "_create_vad", BrokenVad)
    services.settings = dataclasses.replace(services.settings, voice_vad=True)
    services.asr = RecordingAsr()
    events = _turn(live, 40, terminal=("done",))
    assert next(e for e in events if e["type"] == "session")["vad"] == "on"
    assert next(e for e in events if e["type"] == "transcript")["vad"] == "failed"
    assert events[-1]["vad"] == "failed"
