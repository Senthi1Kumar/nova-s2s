import asyncio
import base64
import io
import socket
import threading
import time
import wave

import httpx
import numpy as np
import pytest
import uvicorn
from PIL import Image
from starlette.testclient import TestClient

from aibox.app import create_app
from aibox.services import SlotBusy


@pytest.fixture
def client(services):
    with TestClient(create_app(services)) as c:
        yield c


def png_bytes(w=64, h=48, fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 10, 10)).save(buf, fmt)
    return buf.getvalue()


def wav_bytes(sr=44100, ch=2, secs=0.3):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        t = np.arange(int(sr * secs)) / sr
        s = (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2")
        w.writeframes(np.repeat(s, ch).tobytes())
    return buf.getvalue()


# ---------------- chat ----------------
def test_chat_non_stream(client):
    r = client.post("/v1/chat/completions", json={
        "model": "qwen3:1.7b", "messages": [{"role": "user", "content": "hi"}]})
    j = r.json()
    assert r.status_code == 200
    assert j["choices"][0]["message"]["content"] == "Hello from the box"
    assert j["meta"]["device"] == "npu" and j["usage"]["completion_tokens"] == 4


def test_chat_stream_sse(client):
    r = client.post("/v1/chat/completions", json={
        "model": "qwen3:1.7b", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    lines = [x for x in r.text.splitlines() if x.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    import json
    final = json.loads(lines[-2][5:])
    assert {"meta", "usage", "tokens_per_s"} <= set(final)
    text = "".join(json.loads(x[5:])["choices"][0]["delta"].get("content", "") for x in lines[:-1])
    assert text == "Hello from the box"


def test_chat_max_tokens_cap(client, services):
    client.post("/v1/chat/completions", json={
        "model": "qwen3:1.7b", "max_tokens": 99999,
        "messages": [{"role": "user", "content": "hi"}]})
    assert services.chat.last["max_tokens"] == 512


def test_chat_image_routes_to_vlm(client, services):
    uri = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
    r = client.post("/v1/chat/completions", json={"model": "qwen3-vl", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "what?"},
                                      {"type": "image_url", "image_url": {"url": uri}}]}]})
    assert r.status_code == 200 and services.vlm.calls == 1 and services.chat.calls == 0
    assert "fake scene" in r.json()["choices"][0]["message"]["content"]


def test_chat_errors(client):
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 400
    assert client.post("/v1/chat/completions", json={
        "model": "nope", "messages": [{"role": "user", "content": "x"}]}).status_code == 404
    r = client.post("/v1/chat/completions", json={"model": "qwen3-vl", "messages": [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/y"}}]}]})
    assert r.status_code == 400


def test_chat_slot_busy_is_503(client, services):
    def busy(*a, **k):
        raise SlotBusy(7.0)

    services.chat.stream = busy
    r = client.post("/v1/chat/completions", json={
        "model": "qwen3:1.7b", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and r.headers["retry-after"] == "7"


def test_chat_ws(client):
    with client.websocket_connect("/v1/chat/stream") as ws:
        ws.send_json({"model": "qwen3:1.7b", "messages": [{"role": "user", "content": "hi"}]})
        toks = []
        while True:
            m = ws.receive_json()
            if m["type"] == "token":
                toks.append(m["text"])
            else:
                break
        assert "".join(toks) == "Hello from the box"
        assert m["type"] == "done" and {"meta", "usage", "tokens_per_s", "ttft_ms"} <= set(m)
        ws.send_json({"model": "nope", "messages": [{"role": "user", "content": "x"}]})
        assert ws.receive_json()["type"] == "error"


# ---------------- audio ----------------
def test_transcribe_stereo_44k(client):
    r = client.post("/v1/audio/transcriptions", data={"model": "whisper-base"},
                    files={"file": ("a.wav", wav_bytes(), "audio/wav")})
    j = r.json()
    assert r.status_code == 200 and j["text"] == "hello box"
    assert abs(j["audio_s"] - 0.3) < 0.02 and "meta" in j


def test_transcribe_empty_and_garbage(client):
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"", "audio/wav")})
    assert r.status_code == 400
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.ogg", b"notaudio" * 10, "x")})
    assert r.status_code in (415, 400)


def test_speech(client):
    r = client.post("/v1/audio/speech", json={"input": "hello"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF" and "x-infer-ms" in r.headers
    assert client.post("/v1/audio/speech", json={"input": "  "}).status_code == 400
    assert client.post("/v1/audio/speech",
                       json={"input": "x", "response_format": "mp3"}).status_code == 400


# ---------------- vision ----------------
def test_detect_pose_png(client):
    r = client.post("/v1/vision/detect", data={"task": "pose"},
                    files={"image": ("a.png", png_bytes(), "image/png")})
    j = r.json()
    assert r.status_code == 200 and j["task"] == "pose" and "meta" in j
    assert j["detections"][0]["keypoints"]


def test_detect_json_b64(client):
    r = client.post("/v1/vision/detect", json={
        "image_b64": base64.b64encode(png_bytes()).decode(), "task": "ocr"})
    assert r.status_code == 200 and r.json()["task"] == "ocr"


def test_detect_bad_media(client):
    r = client.post("/v1/vision/detect", data={"task": "pose"},
                    files={"image": ("a.txt", b"hello world", "text/plain")})
    assert r.status_code == 415
    r = client.post("/v1/vision/detect", data={"task": "nope"},
                    files={"image": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 400
    r = client.post("/v1/vision/detect", files={"image": ("a.png", b"", "image/png")})
    assert r.status_code == 400


def test_detect_huge_image_is_resized(client, services):
    r = client.post("/v1/vision/detect", data={"task": "detection"},
                    files={"image": ("a.jpg", png_bytes(4000, 3000, "JPEG"), "image/jpeg")})
    assert r.status_code == 200 and r.json()["image_width"] == 1920


def test_vision_ask(client):
    r = client.post("/v1/vision/ask", data={"prompt": "what?"},
                    files={"image": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 200 and "fake scene" in r.json()["answer"]
    r = client.post("/v1/vision/ask", files={"image": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 400


def test_vision_stream_jpeg(client):
    with client.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "c920e", "task": "detection", "fps": 5})
        b = ws.receive_bytes()
        assert b[:2] == b"\xff\xd8"
        m = ws.receive_json()
        assert m["task"] == "detection" and m["frame_id"] == 1
        assert m["results"]["detections"] and "fps" in m


def test_vision_stream_rgb565(client):
    with client.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "c920e", "task": "detection", "fps": 5,
                      "preview": "rgb565", "preview_width": 320})
        hdr = ws.receive_json()
        assert hdr["type"] == "preview" and hdr["w"] == 320 and hdr["format"] == "rgb565"
        data = ws.receive_bytes()
        assert len(data) == 320 * hdr["h"] * 2
        assert ws.receive_json()["frame_id"] == 1


def test_vision_stream_client_frames(client):
    with client.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "client", "task": "dms", "fps": 20})
        ws.send_bytes(png_bytes(160, 120, "JPEG"))
        m = ws.receive_json()
        assert m["task"] == "dms" and m["results"]["dms"]["faces"] == 1


def test_vision_stream_bad_source(client):
    with client.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "nope", "task": "detection"})
        assert ws.receive_json()["type"] == "error"


def test_vision_stream_degraded_and_cap(client, services):
    services.governor.genai_state = lambda: "generating"
    services.governor.vision_fps_cap = lambda: 2.0
    with client.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "c920e", "task": "detection", "fps": 30, "preview": "none"})
        m1 = ws.receive_json()
        t = time.monotonic()
        ws.receive_json()
        assert m1["degraded"] == "genai_window"
        assert time.monotonic() - t >= 0.4  # capped to 2 fps


# ---------------- voice ----------------
def test_voice_stream(client):
    pcm = (np.zeros(16000, "<i2")).tobytes()
    with client.websocket_connect("/v1/voice/stream") as ws:
        ws.send_json({"type": "start", "model": "qwen3:1.7b"})
        ws.send_bytes(pcm)
        ws.send_json({"type": "stop"})
        msgs = []
        while True:
            m = ws.receive()
            msgs.append(m)
            if m.get("bytes"):
                break
    js = [__import__("json").loads(m["text"]) for m in msgs if m.get("text")]
    kinds = [j["type"] for j in js]
    assert kinds[0] == "transcript" and js[0]["text"] == "hello box"
    assert "token" in kinds and kinds[-1] == "done"
    assert {"asr_ms", "llm_ttft_ms", "llm_ms", "tts_ms"} <= set(js[-1]["timings"])
    assert msgs[-1]["bytes"][:4] == b"RIFF"


def test_voice_empty_stop(client):
    with client.websocket_connect("/v1/voice/stream") as ws:
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "error"


# ---------------- disconnect cancellation (real server) ----------------
class SlowChat:
    def __init__(self):
        self.cancelled = False
        self.started = asyncio.Event() if False else None

    async def stream(self, model, messages, max_tokens, images=None):
        done = False
        try:
            for i in range(1000):
                await asyncio.sleep(0.02)
                yield f"t{i} "
            done = True
        finally:
            if not done:
                self.cancelled = True


class TrackedCams:
    def __init__(self, inner):
        self.inner = inner
        self.open = 0

    def sources(self):
        return self.inner.sources()

    async def frames(self, source, fps):
        self.open += 1
        try:
            async for f in self.inner.frames(source, fps):
                yield f
        finally:
            self.open -= 1


@pytest.fixture
def live(services):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(
        uvicorn.Config(create_app(services), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield port
    server.should_exit = True
    t.join(5)


def _wait(cond, secs=3.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


async def test_sse_disconnect_cancels_generation(live, services):
    services.chat = SlowChat()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{live}", timeout=5) as c:
        async with c.stream("POST", "/v1/chat/completions", json={
                "model": "qwen3:1.7b", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}) as r:
            async for line in r.aiter_lines():
                if line.startswith("data:") and "t1" in line:
                    break
    assert _wait(lambda: services.chat.cancelled)


def test_chat_ws_disconnect_cancels(live, services):
    from websockets.sync.client import connect

    services.chat = SlowChat()
    with connect(f"ws://127.0.0.1:{live}/v1/chat/stream") as ws:
        ws.send('{"model":"qwen3:1.7b","messages":[{"role":"user","content":"hi"}]}')
        ws.recv()
        ws.recv()
    assert _wait(lambda: services.chat.cancelled)


def test_vision_ws_disconnect_releases_camera(live, services):
    from websockets.sync.client import connect

    services.cameras = TrackedCams(services.cameras)
    with connect(f"ws://127.0.0.1:{live}/v1/vision/stream") as ws:
        ws.send('{"source":"c920e","task":"detection","fps":10,"preview":"none"}')
        ws.recv()
        assert services.cameras.open == 1
    assert _wait(lambda: services.cameras.open == 0)


def test_voice_ws_disconnect_cancels(live, services):
    from websockets.sync.client import connect

    services.chat = SlowChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send('{"type":"start"}')
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send('{"type":"stop"}')
        ws.recv()  # transcript
        ws.recv()  # first token
    assert _wait(lambda: services.chat.cancelled)


def test_vision_stream_reports_loading_status_instead_of_error(services):
    services.vision.missing_models = lambda task, model=None: ["yolov5m_seg"]
    with TestClient(create_app(services)) as c, c.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "c920e", "task": "segmentation", "fps": 20, "preview": "none"})
        kinds = []
        for _ in range(10):
            m = ws.receive_json()
            kinds.append(m["type"])
            if m["type"] == "status":
                assert m["state"] == "loading" and m["model"] == "yolov5m_seg"
            if "result" in kinds:
                break
        assert "status" in kinds and "result" in kinds and "error" not in kinds
