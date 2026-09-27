"""nova_client --ui: UI-state lines for a screen (docs/api/ui-state.md)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "nova_client_ui", ROOT / "examples/realtime/python/nova_client.py"
)
nc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nc)


def test_ui_updates_map_realtime_events():
    assert nc.ui_updates({"type": "session.created"}) == [{"conn": "up"}, {"error": ""}]
    for fsm, ui in [("IDLE", "IDLE"), ("ARMED", "LISTENING"), ("LISTENING", "LISTENING"),
                    ("INTERRUPTING", "LISTENING"), ("TRANSCRIBING", "THINKING"),
                    ("THINKING", "THINKING"), ("SPEAKING", "SPEAKING")]:
        assert nc.ui_updates({"type": "nova.fsm", "fsm": {"state": fsm}}) == [{"state": ui}]
    assert nc.ui_updates({"type": "nova.fsm", "fsm": {"state": "WEIRD"}}) == []
    assert nc.ui_updates({"type": "conversation.item.input_audio_transcription.completed",
                          "transcript": "hi"}) == [{"you": "hi"}]
    assert nc.ui_updates({"type": "response.audio_transcript.done",
                          "transcript": "hello"}) == [{"nova": "hello"}]
    assert nc.ui_updates({"type": "error", "error": {"type": "session_limit_reached"}}) == [
        {"error": "session_limit_reached"}]
    assert nc.ui_updates({"type": "response.audio.delta"}) == []


def test_mic_level_scales_rms():
    silence = b"\x00\x00" * 320
    loud = (int(0.2 * 32767)).to_bytes(2, "little", signed=True) * 320
    quiet = (int(0.05 * 32767)).to_bytes(2, "little", signed=True) * 320
    assert nc.mic_level(silence) == 0.0
    assert nc.mic_level(loud) == 1.0                  # rms 0.2 -> clamped
    assert abs(nc.mic_level(quiet) - 0.5) < 0.01      # rms 0.05 / 0.1
    assert nc.mic_level(b"") == 0.0


def _child(code: str) -> str:
    return f'{sys.executable} -c "{code}"'


def test_lines_reach_the_ui_process(tmp_path):
    out = tmp_path / "ui.txt"
    code = ("import sys; f=open(r'%s','w',encoding='utf-8'); "
            "[(f.write(l), f.flush()) for l in sys.stdin]" % out)
    link = nc.UiLink(_child(code), on_wake=lambda: None)
    link.send({"state": "LISTENING"})
    link.send({"you": "hi — ünïcödé 😀"})
    time.sleep(0.5)
    link.close()
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert lines == [{"state": "LISTENING"}, {"you": "hi — ünïcödé 😀"}]


def test_slow_ui_never_blocks_and_keeps_the_newest():
    link = nc.UiLink(_child("import time; time.sleep(5)"), on_wake=lambda: None)
    big = "x" * 1000
    t0 = time.monotonic()
    for i in range(1000):                       # ~1 MB: more than any pipe buffer
        link.send({"nova": f"{i} {big}"})
    elapsed = time.monotonic() - t0
    try:
        assert elapsed < 1.0
        assert len(link.q) <= nc.UI_QUEUE_MAX
        assert json.loads(link.q[-1])["nova"].startswith("999 ")
    finally:
        link.close()


def test_level_is_rate_limited():
    link = nc.UiLink(_child("import time; time.sleep(5)"), on_wake=lambda: None)
    try:
        frame = (int(0.05 * 32767)).to_bytes(2, "little", signed=True) * 320
        for _ in range(50):                     # 50 frames with no time passing
            link.level(frame)
        levels = [line for line in link.q if '"level"' in line]
        assert len(levels) <= 1
    finally:
        link.close()


def test_ui_noise_is_ignored_and_wake_still_arms():
    woke = []
    code = ("print('LVGL log noise'); print('{not json'); "
            "print('{\\\"wake\\\": true}', flush=True); import time; time.sleep(1)")
    link = nc.UiLink(_child(code), on_wake=lambda: woke.append(1))
    time.sleep(0.8)
    link.close()
    assert woke == [1]


def test_dead_ui_never_breaks_the_client():
    link = nc.UiLink(_child("import sys; sys.exit(0)"), on_wake=lambda: None)
    time.sleep(0.3)
    for _ in range(200):                        # must not raise
        link.send({"state": "SPEAKING"})
        link.level(b"\x00\x00" * 320)
    link.close()


def test_device_forwards_events_to_the_ui():
    sent = []

    class _FakeUi:
        def send(self, update):
            sent.append(update)

    class _WS:
        async def send(self, m):
            pass

    dev = nc.Device(_WS(), wake="device", echo_cancellation=False, ui=_FakeUi())
    asyncio.run(dev.on_event({"type": "nova.fsm", "fsm": {"state": "TRANSCRIBING"}}))
    asyncio.run(dev.on_event({"type": "response.audio_transcript.done", "transcript": "ok"}))
    assert sent == [{"state": "THINKING"}, {"nova": "ok"}]


def test_taps_while_connecting_arm_once_when_connected():
    sent = []

    class _WS:
        async def send(self, m):
            sent.append(json.loads(m))

    code = ("import time; print('{\\\"wake\\\": true}'); print('{\\\"wake\\\": true}', flush=True); "
            "time.sleep(1.5); print('{\\\"wake\\\": true}', flush=True); time.sleep(1)")

    async def scenario():
        holder: dict = {}
        link = nc._open_ui(_child(code), asyncio.get_running_loop(), holder)
        try:
            await asyncio.sleep(0.8)               # two taps land while "connecting"
            assert sent == []
            dev = nc.Device(_WS(), wake="device", echo_cancellation=False)
            await nc._ui_connected(holder, dev, dev.ws)
            assert sent == [{"type": "session.arm"}]   # replayed once, not twice
            assert dev.arm_pending is True
            await asyncio.sleep(1.5)               # a tap after connecting arms at once
            assert sent == [{"type": "session.arm"}, {"type": "session.arm"}]
        finally:
            link.close()

    asyncio.run(scenario())


def test_ui_that_cannot_start_is_skipped(capfd):
    loop = asyncio.new_event_loop()
    try:
        for cmd in ("./no/such/nova_face", "nova_face 'unbalanced"):
            assert nc._open_ui(cmd, loop, {}) is None
    finally:
        loop.close()
    assert capfd.readouterr().err.count("[ui] could not start") == 2
