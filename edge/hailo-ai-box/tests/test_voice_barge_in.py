"""Opt-in voice barge-in: a confirmed interruption cancels the reply, anything else is dropped."""

import asyncio
import dataclasses
import io
import json
import time
import wave

import numpy as np
import pytest
from websockets.sync.client import connect

from aibox.voice import pipecat_services
from tests.test_api_inference import live  # noqa: F401

WINDOW = 512
SPEECH = (np.ones(WINDOW * 2) * 1000).astype("<i2").tobytes()
SILENCE = np.zeros(WINDOW * 2, "<i2").tobytes()


class LoudnessVad:
    def voice_confidence(self, buffer):
        return 1.0 if np.abs(np.frombuffer(buffer, "<i2")).mean() > 500 else 0.0


class CountingAsr:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, pcm16k, language=None):
        self.calls += 1
        return f"utterance {self.calls}"


class SlowTts:
    async def synthesize(self, text, voice=None):
        await asyncio.sleep(0.3)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x01\x00" * 8000)
        return buf.getvalue()


class Chat:
    async def stream(self, model, messages, max_tokens, images=None):
        for part in ["One. ", "Two. ", "Three. "]:
            yield part
            await asyncio.sleep(0.05)


@pytest.fixture
def barge(services, monkeypatch):
    def configure(enabled):
        monkeypatch.setattr(pipecat_services, "_create_vad", LoudnessVad)
        services.settings = dataclasses.replace(
            services.settings, voice_vad=True, voice_partials=False, voice_barge_in=enabled)
        services.asr, services.tts, services.chat = CountingAsr(), SlowTts(), Chat()

    return configure


def _first_turn_until_audio(ws):
    ws.send(json.dumps({"type": "start", "protocol": 2}))
    for _ in range(8):
        ws.send(SPEECH)
    ws.send(json.dumps({"type": "stop"}))
    while True:
        if isinstance(ws.recv(timeout=5), bytes):
            return


def _drain(ws, seconds):
    events = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            raw = ws.recv(timeout=0.2)
        except TimeoutError:
            continue
        events.append(raw if isinstance(raw, bytes) else json.loads(raw))
    return events


def _barge(ws, frames, stop=True):
    ws.send(json.dumps({"type": "start", "barge_in": True}))
    for frame in frames:
        ws.send(frame)
    if stop:
        ws.send(json.dumps({"type": "stop"}))


def _types(events):
    return [e["type"] for e in events if isinstance(e, dict)]


def test_confirmed_barge_in_interrupts_and_starts_new_turn(live, barge):  # noqa: F811
    barge(True)
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        _first_turn_until_audio(ws)
        _barge(ws, [SPEECH] * 6)
        events = _drain(ws, 2.5)
    types = _types(events)
    assert types.index("interrupted") < types.index("session")
    interrupted = {"type": "interrupted", "turn_id": 1}
    assert interrupted in events
    after = events[events.index(interrupted) + 1:]
    assert not any(isinstance(e, dict) and e.get("turn_id") == 1 for e in after)
    assert next(e for e in after if isinstance(e, dict) and e["type"] == "session")["turn_id"] == 2
    transcript = next(e for e in after if isinstance(e, dict) and e["type"] == "transcript")
    assert not any(isinstance(e, bytes) for e in after[:after.index(transcript)])
    assert transcript["turn_id"] == 2 and transcript["text"] == "utterance 2"
    assert next(e for e in after if isinstance(e, dict) and e["type"] == "done")["turn_id"] == 2


def test_non_speech_barge_in_is_dropped_and_reply_completes(live, barge):  # noqa: F811
    barge(True)
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        _first_turn_until_audio(ws)
        _barge(ws, [SILENCE] * 6)
        events = _drain(ws, 2.5)
    types = _types(events)
    assert "interrupted" not in types and "session" not in types
    assert next(e for e in events if isinstance(e, dict) and e["type"] == "done")["turn_id"] == 1


def test_rejected_barge_in_then_normal_turn_gets_next_id(live, barge):  # noqa: F811
    barge(True)
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        _first_turn_until_audio(ws)
        _barge(ws, [SILENCE] * 6)
        events = _drain(ws, 2.0)
        ws.send(json.dumps({"type": "start", "protocol": 2}))
        for _ in range(8):
            ws.send(SPEECH)
        ws.send(json.dumps({"type": "stop"}))
        events += _drain(ws, 2.0)
    assert "interrupted" not in _types(events)
    assert next(e for e in events if isinstance(e, dict) and e["type"] == "session")["turn_id"] == 2


def test_barge_in_without_vad_needs_400ms_of_audio(live, barge, services):  # noqa: F811
    barge(True)
    services.settings = dataclasses.replace(services.settings, voice_vad=False)
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send(json.dumps({"type": "start", "protocol": 2}))
        ws.send(SPEECH * 8)
        ws.send(json.dumps({"type": "stop"}))
        while not isinstance(ws.recv(timeout=5), bytes):
            pass
        _barge(ws, [SPEECH] * 6, stop=False)  # 6 x 64 ms = 384 ms
        short = _drain(ws, 0.5)
        ws.send(SPEECH)
        confirmed = _drain(ws, 1.0)
    assert "interrupted" not in _types(short)
    assert {"type": "interrupted", "turn_id": 1} in confirmed


def test_barge_in_off_is_ignored(live, barge):  # noqa: F811
    barge(False)
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        _first_turn_until_audio(ws)
        _barge(ws, [SPEECH] * 6)
        events = _drain(ws, 2.5)
    types = _types(events)
    assert "interrupted" not in types and "session" not in types and "error" not in types
    assert next(e for e in events if isinstance(e, dict) and e["type"] == "done")["turn_id"] == 1


def test_barge_in_setting_env_parsing():
    from aibox.config import load_settings

    assert load_settings({}).voice_barge_in is False
    assert load_settings({"AIBOX_VOICE_BARGE_IN": "1"}).voice_barge_in is True
