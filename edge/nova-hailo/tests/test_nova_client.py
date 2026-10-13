"""Reference client vs a fake nova-realtime/1 server (no pipeline, no audio device)."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import signal
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "nova_client", ROOT / "examples/realtime/python/nova_client.py"
)
nova_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nova_client)


def _write_wav(path: Path, seconds: float, rate: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.zeros(int(seconds * rate), dtype=np.int16).tobytes())


def test_pcm_frames_are_20ms_and_even():
    frames = list(nova_client.pcm_frames(b"\x00" * 1500))
    assert [len(f) for f in frames] == [640, 640, 220]


def _fake_server(log: list):
    async def handler(ws):
        log.append({"auth": ws.request.headers.get("Authorization")})
        await ws.send(json.dumps({"type": "session.created",
                                  "session": {"protocol": "nova-realtime/1"}}))
        await ws.send(json.dumps({"type": "nova.fsm", "fsm": {"state": "IDLE"}}))
        frames = 0
        async for msg in ws:
            if isinstance(msg, bytes):
                log.append({"bin": len(msg)})
                frames += 1
                if frames == 10:
                    reply = np.zeros(1600, dtype=np.int16).tobytes()
                    await ws.send(json.dumps({"type": "nova.fsm", "fsm": {"state": "SPEAKING"}}))
                    await ws.send(json.dumps({"type": "response.audio.delta",
                                              "delta": base64.b64encode(reply).decode(),
                                              "sample_rate": 16000, "generation_id": 1}))
                    await ws.send(json.dumps({"type": "response.done", "response": {}}))
                    await ws.send(json.dumps({"type": "nova.fsm", "fsm": {"state": "IDLE"}}))
            else:
                ev = json.loads(msg)
                log.append(ev)
                if ev["type"] == "session.arm":
                    await ws.send(json.dumps({"type": "nova.fsm", "fsm": {"state": "LISTENING"}}))
                if ev["type"] == "playback.stopped":
                    return
    return handler


# Loopback-only: a fake server on 127.0.0.1 stands in for the Pi.
@pytest.mark.allow_network
def test_run_wav_device_mode_end_to_end(tmp_path):
    wav_in, wav_out = tmp_path / "q.wav", tmp_path / "a.wav"
    _write_wav(wav_in, 0.3)
    log: list = []

    async def go():
        async with serve(_fake_server(log), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            return await nova_client.run_wav(
                f"ws://127.0.0.1:{port}/v1/realtime", "tok", wav_in, wav_out,
                wake="device", echo_cancellation=False, timeout_s=10,
            )

    assert asyncio.run(go()) == 0
    assert log[0] == {"auth": "Bearer tok"}
    update = next(e for e in log if e.get("type") == "session.update")
    assert update["session"]["input"] == {"wake": "device", "echo_cancellation": False}
    assert update["session"]["audio"]["output"]["sample_rate"] == 16000
    kinds = [e.get("type") or ("bin" if "bin" in e else None) for e in log[1:]]
    assert kinds.index("session.arm") < kinds.index("bin")
    assert all(e["bin"] == 640 for e in log if "bin" in e)
    assert kinds.index("playback.started") < kinds.index("playback.stopped")
    with wave.open(str(wav_out)) as w:
        assert w.getframerate() == 16000
        assert w.getnframes() == 1600


class _SentWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, m):
        self.sent.append(json.loads(m))


class _Speaker:
    def __init__(self):
        self.flushed = 0
        self.written = 0

    def write(self, pcm):
        self.written += 1

    def flush(self):
        self.flushed += 1


def _delta(gen):
    return {"type": "response.audio.delta", "delta": base64.b64encode(b"\0" * 640).decode(),
            "sample_rate": 16000, "generation_id": gen}


def test_new_generation_flushes_stale_audio_and_reports_interruption():
    ws, spk = _SentWS(), _Speaker()
    d = nova_client.Device(ws, wake="device", echo_cancellation=True)

    async def go():
        await d.on_event(_delta(1), spk)
        await d.on_event(_delta(2), spk)

    asyncio.run(go())
    assert [m["type"] for m in ws.sent] == ["playback.started", "playback.interrupted", "playback.started"]
    assert spk.flushed == 1
    assert len(d.reply) == 1  # only the live generation is kept


def test_cancelled_response_flushes_without_playback_stopped():
    ws, spk = _SentWS(), _Speaker()
    d = nova_client.Device(ws, wake="device", echo_cancellation=True)

    async def go():
        await d.on_event(_delta(1), spk)
        await d.on_event({"type": "response.done", "response": {"status": "cancelled"}}, spk)

    asyncio.run(go())
    assert [m["type"] for m in ws.sent] == ["playback.started", "playback.interrupted"]
    assert spk.flushed == 1
    assert not d.done.is_set()  # a barge-in is not the end of the exchange


_LIVE_DRIVER = r'''
import asyncio, importlib.util, sys, types

class _Stream:                      # stands in for sounddevice's raw streams
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def start(self): pass
    def stop(self): pass
    def write(self, b): pass

sys.modules["sounddevice"] = types.SimpleNamespace(RawInputStream=_Stream, RawOutputStream=_Stream)

class _WS:                          # a server that stays quiet
    async def send(self, m): pass
    async def close(self): pass
    def __aiter__(self): return self
    async def __anext__(self): await asyncio.Event().wait()

spec = importlib.util.spec_from_file_location("nova_client", sys.argv[1])
nc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nc)

async def _fake_open(url, token, ca):
    return _WS()

nc._open = _fake_open
sys.exit(nc.main(["--wake", "device"]))
'''


def test_one_ctrl_c_stops_live_device_mode():
    # stdin stays open and unread, like a terminal where nobody pressed Enter.
    p = subprocess.Popen([sys.executable, "-c", _LIVE_DRIVER,
                          str(ROOT / "examples/realtime/python/nova_client.py")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "press Enter, then speak"
        p.send_signal(signal.SIGINT)
        try:
            p.wait(timeout=5)  # not communicate(): that would close stdin
        except subprocess.TimeoutExpired:
            pytest.fail("one Ctrl-C did not stop the client within 5 s")
        err = p.stderr.read()
    finally:
        if p.poll() is None:
            p.kill()
        p.communicate()
    assert p.returncode == 130, err
    assert "Traceback" not in err


def test_enter_presses_arm_and_end_of_input_stops_the_reader():
    r, w = os.pipe()
    os.write(w, b"\n\n")
    os.close(w)
    presses = []

    async def go():
        t = nova_client._on_enter(asyncio.get_running_loop(), lambda: presses.append(1), fd=r)
        t.join(1)
        assert t.daemon and not t.is_alive()  # end of input ends the reader
        await asyncio.sleep(0.01)             # run the callbacks it queued

    try:
        asyncio.run(go())
    finally:
        os.close(r)
    assert presses == [1, 1]


def test_device_option_selects_input_and_output():
    class FakeSD:
        class default:
            device = None

    nova_client.select_device(FakeSD, "pulse")
    assert FakeSD.default.device == ("pulse", "pulse")
    nova_client.select_device(FakeSD, "3")
    assert FakeSD.default.device == (3, 3)
    nova_client.select_device(FakeSD, "")
    assert FakeSD.default.device == (3, 3)  # empty leaves the default alone


def test_open_mic_leaves_the_wake_choice_to_the_server():
    update = json.loads(nova_client._session_update("none", True))
    assert "wake" not in update["session"]["input"]
    assert update["session"]["input"]["echo_cancellation"] is True
    d = nova_client.Device(None, wake="none", echo_cancellation=True)
    d.state = "IDLE"
    assert d.should_stream()


def test_open_mic_warns_when_the_server_still_gates_on_its_wake_word(capsys):
    d = nova_client.Device(None, wake="none", echo_cancellation=True)
    asyncio.run(d.on_event({"type": "session.created", "session": {"wake_source": "server"}}))
    assert "wake.mode" in capsys.readouterr().out
    asyncio.run(d.on_event({"type": "session.created", "session": {"wake_source": "none"}}))
    assert capsys.readouterr().out == ""


def test_a_muted_cabin_stops_the_mic_stream():
    d = nova_client.Device(None, wake="none", echo_cancellation=False)
    d.state = "LISTENING"
    asyncio.run(d.on_event({"type": "nova.mic", "muted": True, "source": "cockpit"}))
    assert d.muted is True and not d.should_stream()
    asyncio.run(d.on_event({"type": "nova.mic", "muted": False, "source": "cockpit"}))
    assert d.should_stream()
