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
        assert _recv_json(ws) == {"type": "cancelled"}
        ws.send('{"type":"cancel"}')
        assert _recv_json(ws) == {"type": "cancelled"}


def test_cancel_during_turn_stops_everything(live, services):  # noqa: F811
    services.chat = SlowChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"start"}')
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send('{"type":"stop"}')
        assert _recv_json(ws)["type"] == "transcript"
        assert _recv_json(ws)["type"] == "token"
        ws.send('{"type":"cancel"}')
        seen = []
        while True:
            m = _recv_json(ws)
            seen.append(m)
            if m == {"type": "cancelled"}:
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
        assert _recv_json(ws) == {"type": "cancelled"}


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
