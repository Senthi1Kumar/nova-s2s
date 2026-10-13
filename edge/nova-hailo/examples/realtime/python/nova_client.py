#!/usr/bin/env python3
"""Nova realtime reference client -- a laptop behaving like an audio-only device.

Speaks nova-realtime/1 over one WebSocket and imports nothing from nova_hailo,
so anything it does, an ESP32 or a phone can do too. See docs/api/realtime.md.

  File in, file out (repeatable test):
    nova_client.py --url wss://192.168.1.36:8767/v1/realtime --token $T \
        --ca cert.pem --wav question.wav --out reply.wav --wake device
  Live mic + speaker:
    nova_client.py --url ... --token $T --ca cert.pem --wake server   # say "Hey Nova, ..."
    nova_client.py --url ... --token $T --ca cert.pem --wake device   # Enter = local wake
    nova_client.py --url ... --wake none     # open mic when the server runs without a wake word
  Record a 16 kHz fixture from this laptop's mic:
    nova_client.py --record question.wav --seconds 4
  With a screen (see docs/api/ui-state.md):
    nova_client.py --url ... --wake device --ui examples/realtime/lvgl/build/nova_face
"""
from __future__ import annotations

import argparse
import array
import asyncio
import base64
import collections
import json
import math
import os
import queue
import shlex
import ssl
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

from websockets.asyncio.client import connect

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * 2 * FRAME_MS // 1000  # 640
SILENCE_TAIL_S = 1.5  # lets server VAD see the end of speech in --wav mode
STREAM_STATES_NO_AEC = {"ARMED", "LISTENING", "TRANSCRIBING", "THINKING"}

UI_QUEUE_MAX = 64
UI_LEVEL_HZ = 10
# nova.fsm has more states than a screen needs (docs/api/ui-state.md).
_UI_STATES = {
    "IDLE": "IDLE",
    "ARMED": "LISTENING",
    "LISTENING": "LISTENING",
    "INTERRUPTING": "LISTENING",
    "TRANSCRIBING": "THINKING",
    "THINKING": "THINKING",
    "SPEAKING": "SPEAKING",
}


def ui_updates(ev: dict) -> list[dict]:
    """UI-state updates (docs/api/ui-state.md) for one realtime event."""
    t = ev.get("type")
    if t == "session.created":
        return [{"conn": "up"}, {"error": ""}]
    if t == "nova.fsm":
        state = _UI_STATES.get(str((ev.get("fsm") or {}).get("state") or ""))
        return [{"state": state}] if state else []
    if t == "conversation.item.input_audio_transcription.completed":
        return [{"you": str(ev.get("transcript") or "")}]
    if t == "response.audio_transcript.done":
        return [{"nova": str(ev.get("transcript") or "")}]
    if t == "error":
        return [{"error": str((ev.get("error") or {}).get("type") or "error")}]
    return []


def mic_level(pcm: bytes) -> float:
    """Orb loudness: min(1, rms / 0.1) of PCM16 scaled to -1..1."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    if sys.byteorder == "big":
        samples.byteswap()                     # the wire format is little-endian
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    return min(1.0, rms / 0.1)


class UiLink:
    """A screen process fed UI-state JSON lines (docs/api/ui-state.md).

    The client never waits on it: lines queue up to UI_QUEUE_MAX and the
    oldest are dropped, so a stalled or dead UI cannot delay the audio.
    """

    def __init__(self, command: str, on_wake: Callable[[], None]):
        self.on_wake = on_wake
        self.q: collections.deque[str] = collections.deque(maxlen=UI_QUEUE_MAX)
        self.cv = threading.Condition()
        self.closed = False
        self._pcm = bytearray()
        self._level_at = 0.0
        self.proc = subprocess.Popen(
            shlex.split(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._write_loop, daemon=True).start()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def send(self, update: dict) -> None:
        with self.cv:
            if self.closed:
                return
            self.q.append(json.dumps(update, ensure_ascii=False))
            self.cv.notify()

    def level(self, pcm: bytes) -> None:
        self._pcm += pcm
        now = time.monotonic()
        if now - self._level_at >= 1.0 / UI_LEVEL_HZ:
            self.send({"level": round(mic_level(bytes(self._pcm)), 3)})
            self._pcm.clear()
            self._level_at = now

    def _write_loop(self) -> None:
        while True:
            with self.cv:
                while not self.q and not self.closed:
                    self.cv.wait()
                if self.closed:
                    return
                line = self.q.popleft()
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                print("[ui] screen process stopped; continuing without it",
                      file=sys.stderr, flush=True)
                self.close()
                return

    def _read_loop(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                        # library logs on stdout are not ours
            if isinstance(msg, dict) and msg.get("wake") is True:
                self.on_wake()

    def close(self) -> None:
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        if self.proc.poll() is None:
            self.proc.terminate()


def pcm_frames(pcm: bytes, frame_bytes: int = FRAME_BYTES) -> Iterator[bytes]:
    for i in range(0, len(pcm) - len(pcm) % 2, frame_bytes):
        yield pcm[i:i + frame_bytes]


def _ssl_context(url: str, ca: str | None) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    # Verification stays on: the bearer token rides in the WebSocket handshake,
    # so the peer must be proven to be the Pi *before* anything is sent. The
    # Pi's self-signed cert.pem is its own trust anchor.
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


def _session_update(wake: str, echo_cancellation: bool) -> str:
    # "none" declares no wake word, so the server keeps its own (wake.mode: ptt = open mic).
    inp: dict = {"echo_cancellation": echo_cancellation}
    if wake != "none":
        inp["wake"] = wake
    return json.dumps({
        "type": "session.update",
        "session": {"input": inp, "audio": {"output": {"sample_rate": RATE}}},
    })


class Device:
    """What a device must track: server state, playback acks, the reply audio."""

    def __init__(self, ws, *, wake: str, echo_cancellation: bool, ui: "UiLink | None" = None):
        self.ws = ws
        self.wake = wake
        self.aec = echo_cancellation
        self.ui = ui
        self.state = "IDLE"
        self.arm_pending = False
        self.playing = False
        self.play_started = 0.0
        self.play_seconds = 0.0
        self.reply: list[bytes] = []
        self.reply_rate = RATE
        self.generation = None
        self.done = asyncio.Event()
        self.muted = False  # the cabin mic, from nova.mic

    def should_stream(self) -> bool:
        if self.muted:
            return False  # the cabin mic is off (nova.mic)
        if self.wake in ("server", "none"):
            return True
        if self.arm_pending:
            return True
        if self.aec:
            return self.state != "IDLE"
        return self.state in STREAM_STATES_NO_AEC

    async def on_event(self, ev: dict, speaker=None) -> None:
        if self.ui is not None:
            for update in ui_updates(ev):
                self.ui.send(update)
        t = ev.get("type")
        if t == "nova.mic":
            self.muted = bool(ev.get("muted"))
            print(f"[mic] {'muted' if self.muted else 'on'} ({ev.get('source')})", flush=True)
        elif t == "session.created" and self.wake == "none":
            if (ev.get("session") or {}).get("wake_source") == "server":
                print("[wake] this server still waits for 'Hey Nova': set wake.mode: ptt in its "
                      "config and restart it for an open mic", flush=True)
        elif t == "nova.fsm":
            self.state = (ev.get("fsm") or {}).get("state", self.state)
            if self.state != "IDLE":
                self.arm_pending = False
        elif t == "wake.detected":
            print(f"[wake] {ev.get('source', 'server')} score={ev.get('score')}", flush=True)
        elif t == "response.audio.delta":
            pcm = base64.b64decode(ev["delta"])
            self.reply_rate = int(ev.get("sample_rate") or RATE)
            gen = ev.get("generation_id")
            if self.playing and gen != self.generation:
                # A new generation means the old reply was cancelled (barge-in):
                # drop what is still queued instead of talking over the user.
                await self._interrupt(speaker)
            self.generation = gen
            if not self.playing:
                self.playing = True
                self.play_started = time.monotonic()
                self.play_seconds = 0.0
                await self.ws.send(json.dumps({"type": "playback.started"}))
            self.play_seconds += len(pcm) / 2 / self.reply_rate
            self.reply.append(pcm)
            if speaker is not None:
                speaker.write(pcm)
        elif t == "conversation.item.input_audio_transcription.completed":
            print(f"you : {ev.get('transcript', '')}", flush=True)
        elif t == "response.audio_transcript.done":
            print(f"nova: {ev.get('transcript', '')}", flush=True)
        elif t == "response.done":
            if (ev.get("response") or {}).get("status") == "cancelled":
                if self.playing:
                    await self._interrupt(speaker)
                return
            if self.playing:
                # Ack only when the speaker would really be quiet: the server
                # holds its echo gate until this arrives.
                remain = self.play_started + self.play_seconds - time.monotonic()
                if remain > 0:
                    await asyncio.sleep(remain)
                self.playing = False
                await self.ws.send(json.dumps({"type": "playback.stopped"}))
            self.done.set()
        elif t == "error":
            print(f"[error] {ev.get('error')}", file=sys.stderr, flush=True)

    async def _interrupt(self, speaker) -> None:
        if speaker is not None:
            speaker.flush()
        self.reply.clear()
        self.playing = False
        await self.ws.send(json.dumps({"type": "playback.interrupted"}))


class Player:
    """Speaker output on its own thread, so playback never blocks the socket."""

    def __init__(self, sd):
        self.q: queue.Queue[bytes] = queue.Queue()
        self.out = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16")
        self.out.start()
        threading.Thread(target=self._run, daemon=True).start()

    def write(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), FRAME_BYTES):
            self.q.put(pcm[i:i + FRAME_BYTES])

    def flush(self) -> None:
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        while True:
            self.out.write(self.q.get())

    def stop(self) -> None:
        self.flush()
        self.out.stop()


async def _open(url, token, ca):
    return await connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        ssl=_ssl_context(url, ca),
        max_size=None,
    )


async def _reader(ws, device: Device, speaker=None):
    async for msg in ws:
        if isinstance(msg, str):
            await device.on_event(json.loads(msg), speaker)


def _open_ui(ui_cmd, loop, holder: dict) -> "UiLink | None":
    """Start the screen; a tap arms the session like a local wake word."""
    if not ui_cmd:
        return None

    async def arm():
        device, ws = holder.get("device"), holder.get("ws")
        if device is None or ws is None:
            holder["wake_pending"] = True      # socket still opening: replayed by _ui_connected
            return
        device.arm_pending = True
        await ws.send(json.dumps({"type": "session.arm"}))
        print("[wake] local (screen)", flush=True)

    holder["arm"] = arm
    try:
        link = UiLink(ui_cmd, on_wake=lambda: loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(arm())))
    except (OSError, ValueError) as e:
        print(f"[ui] could not start {ui_cmd!r} ({e}); continuing without a screen",
              file=sys.stderr, flush=True)
        return None
    link.send({"conn": "connecting"})
    return link


async def _ui_connected(holder: dict, device: Device, ws) -> None:
    """The session is configured: screen taps arm it from now on, and a tap
    that landed while the socket was still opening is replayed, not lost."""
    holder.update(device=device, ws=ws)
    if holder.pop("wake_pending", False):
        await holder["arm"]()


def _on_enter(loop, callback: Callable[[], object], fd: int | None = None) -> threading.Thread:
    """Call `callback` on the loop once per Enter press; stop at end of input.

    A daemon thread, so a read still waiting for Enter never holds up exit
    (a thread-pool read did: Ctrl-C then needed Enter or two more Ctrl-Cs).
    os.read skips stdin's buffer lock, which this thread would otherwise
    hold while the interpreter shuts down."""
    fd = sys.stdin.fileno() if fd is None else fd

    def run():
        while True:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                return
            if not chunk:
                return
            for _ in range(chunk.count(b"\n")):
                try:
                    loop.call_soon_threadsafe(callback)
                except RuntimeError:           # the loop is closed: exiting
                    return

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit PCM WAV")
        return w.readframes(w.getnframes())


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def run_wav(url, token, wav_in, wav_out, *, wake, echo_cancellation,
                  ca=None, timeout_s=60.0, ui_cmd=None) -> int:
    pcm = _read_wav(Path(wav_in))
    holder: dict = {}
    ui = _open_ui(ui_cmd, asyncio.get_running_loop(), holder)
    ws = await _open(url, token, ca)
    device = Device(ws, wake=wake, echo_cancellation=echo_cancellation, ui=ui)
    reader = asyncio.create_task(_reader(ws, device))
    try:
        await ws.send(_session_update(wake, echo_cancellation))
        if wake == "device":
            device.arm_pending = True
            await ws.send(json.dumps({"type": "session.arm"}))
        await _ui_connected(holder, device, ws)
        silence = b"\x00" * FRAME_BYTES
        tail = [silence] * int(SILENCE_TAIL_S * 1000 / FRAME_MS)
        frames = iter(list(pcm_frames(pcm)) + tail)
        deadline = time.monotonic() + timeout_s
        while not device.done.is_set() and time.monotonic() < deadline:
            frame = next(frames, silence)
            if ui is not None:
                ui.level(frame)
            if device.should_stream():
                await ws.send(frame)
            await asyncio.sleep(FRAME_MS / 1000)  # real-time pace, like a mic
        if not device.done.is_set():
            print("timeout: no response.done", file=sys.stderr)
            return 2
        _write_wav(Path(wav_out), b"".join(device.reply), device.reply_rate)
        secs = sum(map(len, device.reply)) / 2 / device.reply_rate
        print(f"reply: {wav_out} ({secs:.2f}s)")
        return 0 if device.reply else 3
    finally:
        reader.cancel()
        await ws.close()
        if ui is not None:
            ui.send({"conn": "down"})
            time.sleep(0.2)                    # let the last lines reach the screen
            ui.close()


async def run_live(url, token, *, wake, echo_cancellation, ca=None, ui_cmd=None) -> int:
    import sounddevice as sd

    loop = asyncio.get_running_loop()
    mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    player = Player(sd)
    holder: dict = {}
    ui = _open_ui(ui_cmd, loop, holder)
    ws = await _open(url, token, ca)
    device = Device(ws, wake=wake, echo_cancellation=echo_cancellation, ui=ui)

    def on_mic(indata, frames, t, status):
        loop.call_soon_threadsafe(lambda b=bytes(indata): mic.full() or mic.put_nowait(b))

    async def arm_from_enter():
        device.arm_pending = True
        await ws.send(json.dumps({"type": "session.arm"}))
        print("[wake] local (Enter)", flush=True)

    reader = asyncio.create_task(_reader(ws, device, speaker=player))
    await ws.send(_session_update(wake, echo_cancellation))
    await _ui_connected(holder, device, ws)
    if wake == "device":
        _on_enter(loop, lambda: asyncio.ensure_future(arm_from_enter()))
    print({"server": "say 'Hey Nova, ...'", "device": "press Enter, then speak"}.get(
        wake, "open mic: just speak"), flush=True)
    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                           blocksize=FRAME_BYTES // 2, callback=on_mic):
        try:
            while not reader.done():
                frame = await mic.get()
                if ui is not None:
                    ui.level(frame)
                if device.should_stream():
                    await ws.send(frame)
        finally:
            reader.cancel()
            await ws.close()
            player.stop()
            if ui is not None:
                ui.send({"conn": "down"})
                ui.close()
    return 0


def record(path: str, seconds: float) -> int:
    import sounddevice as sd

    print(f"recording {seconds:.1f}s ...", flush=True)
    audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="int16")
    sd.wait()
    _write_wav(Path(path), audio.tobytes(), RATE)
    print(f"saved {path}")
    return 0


def select_device(sd, name: str) -> None:
    """Use one device for mic and speaker; a number selects by PortAudio index."""
    if not name:
        return
    dev = int(name) if name.isdigit() else name
    sd.default.device = (dev, dev)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:8766/v1/realtime")
    p.add_argument("--token", default="")
    p.add_argument("--ca", help="PEM to trust (the Pi's self-signed cert.pem)")
    p.add_argument("--wake", choices=["server", "device", "none"], default="server",
                   help="server: say 'Hey Nova'; device: Enter wakes; none: open mic "
                        "(the server's own wake.mode decides)")
    p.add_argument("--aec", action="store_true", help="declare echo_cancellation=true")
    p.add_argument("--wav", help="16 kHz mono WAV to send instead of the mic")
    p.add_argument("--out", default="reply.wav")
    p.add_argument("--record", help="record a fixture WAV from the mic and exit")
    p.add_argument("--seconds", type=float, default=4.0)
    p.add_argument("--device", default=os.environ.get("NOVA_CLIENT_DEVICE", ""),
                   help="sounddevice input/output device (name or index), e.g. pulse to use "
                        "PipeWire's echo canceller via PULSE_SOURCE/PULSE_SINK")
    p.add_argument("--ui", metavar="CMD",
                   help='screen to run and feed UI-state lines, e.g. "examples/realtime/lvgl/build/nova_face"')
    a = p.parse_args(argv)
    if a.device:
        import sounddevice as sd

        select_device(sd, a.device)
    try:
        if a.record:
            return record(a.record, a.seconds)
        if a.wav:
            return asyncio.run(run_wav(a.url, a.token, a.wav, a.out, wake=a.wake,
                                       echo_cancellation=a.aec, ca=a.ca, ui_cmd=a.ui))
        return asyncio.run(run_live(a.url, a.token, wake=a.wake, echo_cancellation=a.aec,
                                    ca=a.ca, ui_cmd=a.ui))
    except KeyboardInterrupt:  # Ctrl-C: the session has already closed cleanly
        print("\nstopped", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
