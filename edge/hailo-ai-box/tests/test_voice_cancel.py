"""Voice WS `cancel`: in-turn cancel stops tokens/audio, out-of-turn is a no-op reply."""

import json
import time

import numpy as np
from websockets.sync.client import connect

from tests.test_api_inference import SlowChat, _wait, live  # noqa: F401 - live is a fixture


def _recv_json(ws):
    m = ws.recv(timeout=5)
    return json.loads(m) if isinstance(m, str) else m


def test_cancel_outside_turn_replies_cancelled(live):  # noqa: F811
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"cancel"}')
        assert _recv_json(ws) == {"type": "cancelled", "turn_id": 0}
        ws.send('{"type":"cancel"}')
        assert _recv_json(ws) == {"type": "cancelled", "turn_id": 0}


def test_cancel_during_turn_stops_everything(live, services):  # noqa: F811
    services.chat = SlowChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"start"}')
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send('{"type":"stop"}')
        assert _recv_json(ws)["type"] == "session"
        event = _recv_json(ws)
        assert event["type"] in {"transcript_partial", "transcript"}
        if event["type"] == "transcript_partial":
            assert _recv_json(ws)["type"] == "transcript"
        assert _recv_json(ws)["type"] == "token"
        ws.send('{"type":"cancel"}')
        seen = []
        while True:
            m = _recv_json(ws)
            seen.append(m)
            if m.get("type") == "cancelled":
                break
        assert all(isinstance(m, dict) and m["type"] == "token" for m in seen[:-1])
        assert _wait(lambda: services.chat.cancelled)
        time.sleep(0.3)  # nothing (token, done, audio) may follow `cancelled`
        try:
            extra = ws.recv(timeout=0.2)
        except TimeoutError:
            extra = None
        assert extra is None
        # the connection stays usable: next turn works
        ws.send('{"type":"cancel"}')
        assert _recv_json(ws) == {"type": "cancelled", "turn_id": 1}


def test_cancel_after_done_sends_no_audio_after_cancelled(live, services):  # noqa: F811
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"start"}')
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send('{"type":"stop"}')
        ws.send('{"type":"cancel"}')
        got_cancel = False
        while True:
            try:
                m = ws.recv(timeout=1.0)
            except TimeoutError:
                break
            assert not (got_cancel and isinstance(m, bytes)), "audio after cancelled"
            assert not (got_cancel and json.loads(m)["type"] != "cancelled")
            if isinstance(m, str) and json.loads(m)["type"] == "cancelled":
                got_cancel = True
        assert got_cancel


def test_cancel_mid_reply_drops_tts_end_done_and_audio(live, services):  # noqa: F811
    import asyncio

    class SlowTts:
        async def synthesize(self, text, voice=None):
            await asyncio.sleep(0.4)
            import io
            import wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x01\x00" * 8000)
            return buf.getvalue()

    class Chat:
        async def stream(self, model, messages, max_tokens, images=None):
            for part in ["One. ", "Two. ", "Three. ", "Four. "]:
                yield part
                await asyncio.sleep(0.05)

    services.chat, services.tts = Chat(), SlowTts()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"start"}')
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send('{"type":"stop"}')
        while True:
            m = _recv_json(ws)
            if isinstance(m, dict) and m["type"] == "token":
                break
        ws.send('{"type":"cancel"}')
        while True:
            m = _recv_json(ws)
            if isinstance(m, dict) and m["type"] == "cancelled":
                break
        end = time.time() + 1.5
        while time.time() < end:
            try:
                extra = ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            assert not isinstance(extra, bytes), "audio after cancelled"
            assert json.loads(extra)["type"] not in {"tts_end", "done", "tts_start"}


def test_cancel_with_queued_audio_drops_remaining_turn_output(live, services):  # noqa: F811
    import asyncio
    import io
    import wave

    class LongTts:
        async def synthesize(self, text, voice=None):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x01\x00" * 16000 * 10)
            return buf.getvalue()

    class Chat:
        async def stream(self, model, messages, max_tokens, images=None):
            for part in ["One. ", "Two. ", "Three. "]:
                yield part
                await asyncio.sleep(0.02)

    services.chat, services.tts = Chat(), LongTts()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"start"}')
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send('{"type":"stop"}')
        saw_start = saw_audio = False
        while not (saw_start and saw_audio):
            m = ws.recv(timeout=5)
            if isinstance(m, bytes):
                saw_audio = saw_start
            elif json.loads(m)["type"] == "tts_start":
                saw_start = True
        ws.send('{"type":"cancel"}')
        cancelled = False
        while not cancelled:
            m = ws.recv(timeout=5)
            cancelled = isinstance(m, str) and json.loads(m)["type"] == "cancelled"
        time.sleep(0.1)
        end = time.time() + 1.0
        while time.time() < end:
            try:
                extra = ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            assert not isinstance(extra, bytes), "audio after cancelled"
            assert json.loads(extra)["type"] not in {"tts_start", "tts_end", "done"}


def test_cancel_native_stream_drops_remaining_turn_output(live, services):  # noqa: F811
    import asyncio

    class NativeTts:
        async def synthesize_stream(self, text, voice=None):
            for _ in range(20):
                await asyncio.sleep(0.05)
                yield b"\x01\x00" * 2400, 24000

    class Chat:
        async def stream(self, model, messages, max_tokens, images=None):
            for part in ["One. ", "Two. ", "Three. "]:
                yield part
                await asyncio.sleep(0.02)

    services.chat, services.tts = Chat(), NativeTts()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"start"}')
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send('{"type":"stop"}')
        saw_start = saw_audio = False
        while not (saw_start and saw_audio):
            m = ws.recv(timeout=5)
            if isinstance(m, bytes):
                saw_audio = saw_start
            elif json.loads(m)["type"] == "tts_start":
                saw_start = True
        ws.send('{"type":"cancel"}')
        cancelled = False
        while not cancelled:
            m = ws.recv(timeout=5)
            cancelled = isinstance(m, str) and json.loads(m)["type"] == "cancelled"
        end = time.time() + 1.0
        while time.time() < end:
            try:
                extra = ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            assert not isinstance(extra, bytes), "native audio after cancelled"
            assert json.loads(extra)["type"] not in {"tts_start", "tts_end", "done"}
