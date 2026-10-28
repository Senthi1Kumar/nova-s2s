import httpx

from aibox.api.deps import timed_meta
from aibox.app import create_app
from aibox.config import Settings
from aibox.fakes import make_fake_services
from aibox.telemetry import Telemetry


def _client(services, host="testclient"):
    transport = httpx.ASGITransport(app=create_app(services), client=(host, 1234))
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_request_is_inspected(services):
    async with _client(services) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    rows = [e for e in services.inspector.recent() if e.get("endpoint") == "/health"]
    assert len(rows) == 1 and rows[0]["status"] == 200
    assert rows[0]["client"] == "testclient" and rows[0]["bytes_out"] > 0


async def test_api_key_required_off_loopback():
    s = make_fake_services(Settings(fake=True, api_key="k"))
    async with _client(s, "10.0.0.5") as c:
        assert (await c.get("/health")).status_code == 401
        assert (await c.get("/health", headers={"Authorization": "Bearer k"})).status_code == 200
        assert (await c.get("/health?key=k")).status_code == 200
        assert (await c.get("/health", headers={"Authorization": "Bearer x"})).status_code == 401
    async with _client(s, "127.0.0.1") as c:
        assert (await c.get("/health")).status_code == 200
    assert s.inspector.clients()


def test_telemetry_snapshot_shape(services):
    snap = Telemetry(services).snapshot()
    assert set(snap) == {"t", "host", "npu", "models", "server"}
    assert {"cpu_pct", "cpu_per_core", "mem_used_mb", "mem_total_mb", "temp_c", "arm_mhz",
            "throttled", "load_1m"} <= set(snap["host"])
    assert {"genai_owner", "genai_state", "nnc_util_pct"} <= set(snap["npu"])
    assert {"rps", "clients", "requests_total", "uptime_s"} <= set(snap["server"])
    assert snap["host"]["mem_total_mb"] > 0


def test_timed_meta():
    import time

    m = timed_meta("m", "npu", time.perf_counter() - 0.05, 20.0)
    assert set(m) == {"model", "device", "queue_ms", "infer_ms", "request_id"}
    assert m["queue_ms"] >= 25 and m["infer_ms"] == 20.0


async def test_telemetry_rps_is_thread_safe(services):
    import asyncio
    import threading

    tel = Telemetry(services)
    stop = threading.Event()
    errors = []

    def hammer():
        while not stop.is_set():
            tel.note_request()

    th = threading.Thread(target=hammer)
    th.start()
    try:
        for _ in range(300):
            try:
                tel._rps()
            except RuntimeError as e:  # "deque mutated during iteration"
                errors.append(e)
            await asyncio.sleep(0)
    finally:
        stop.set()
        th.join()
    assert not errors


async def test_telemetry_run_survives_bad_sample(services, monkeypatch):
    import asyncio

    tel = Telemetry(services)
    real = tel.snapshot
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real()

    monkeypatch.setattr(tel, "snapshot", flaky)
    task = asyncio.create_task(tel.run(interval=0.01))
    await asyncio.sleep(0.2)
    task.cancel()
    assert calls["n"] >= 2 and tel.latest is not None


async def test_telemetry_current_stays_off_the_loop(services, monkeypatch):
    import threading

    tel = Telemetry(services)
    seen = {}
    real = tel.snapshot

    def spy():
        seen["thread"] = threading.current_thread()
        return real()

    monkeypatch.setattr(tel, "snapshot", spy)
    await tel.current()
    assert seen["thread"] is not threading.main_thread()


async def test_unhandled_error_is_json_500_and_inspected(services):
    async def boom(*a, **k):
        raise RuntimeError("model failed")

    services.vision.detect = boom
    async with _client(services) as c:
        r = await c.post("/v1/vision/detect", json={"image_b64": _png_b64(), "task": "pose"})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal" and "model failed" in r.json()["error"]["message"]
    rows = [e for e in services.inspector.recent() if e.get("endpoint") == "/v1/vision/detect"]
    assert rows[-1]["status"] == 500


def _png_b64():
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def test_content_length_over_cap_is_413(services):
    async with _client(services) as c:
        r = await c.post("/v1/audio/transcriptions", content=b"x" * (27 * 1024 * 1024),
                         headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


async def test_chunked_body_over_cap_is_413(services):
    async def gen():
        for _ in range(28):
            yield b"x" * (1024 * 1024)

    async with _client(services) as c:
        r = await c.post("/v1/chat/completions", content=gen(),
                         headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_oversized_data_uri_rejected(services, monkeypatch):
    from aibox.api import chat

    monkeypatch.setattr(chat, "MAX_IMAGE_B64", 100)
    uri = "data:image/png;base64," + _png_b64() * 5
    async with _client(services) as c:
        r = await c.post("/v1/chat/completions", json={"model": "qwen3-vl", "messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": uri}}]}]})
    assert r.status_code == 413


def test_ffmpeg_output_is_bounded(monkeypatch):
    import subprocess

    from aibox import media

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, b"\x00" * 8, b"")

    monkeypatch.setattr(media.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", fake_run)
    media.decode_audio(b"OggS" + b"0" * 32)
    cmd = seen["cmd"]
    assert cmd[cmd.index("-t") + 1] == "121"
    assert cmd.index("-t") < cmd.index("pipe:1")


def test_listener_hardening():
    from aibox.__main__ import listener_configs

    for c in listener_configs(Settings()):
        assert c.proxy_headers is False and c.access_log is False
        assert c.timeout_graceful_shutdown == 3
