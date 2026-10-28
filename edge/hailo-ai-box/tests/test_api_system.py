import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

from aibox.app import create_app


@pytest.fixture
def client(services):
    with TestClient(create_app(services)) as c:
        yield c


@pytest.fixture
def live_url(services):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(
        uvicorn.Config(create_app(services), host="127.0.0.1", port=port, log_level="error")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def test_health(client):
    j = client.get("/health").json()
    assert j["status"] == "ok" and "qwen3-vl" in j["models_loaded"] and "uptime_s" in j


def test_models_list(client):
    j = client.get("/v1/models").json()
    assert j["object"] == "list" and len(j["data"]) >= 15
    for m in j["data"]:
        assert {"id", "modality", "runtime", "state"} <= set(m)


def test_metrics(client):
    j = client.get("/v1/system/metrics").json()
    assert {"t", "host", "npu", "models", "server"} <= set(j)


def test_system_stream_two_snapshots(client, services):
    t0 = time.time()
    with client.websocket_connect("/v1/system/stream") as ws:
        snaps = []
        while len(snaps) < 2:
            m = ws.receive_json()
            if m.get("kind") != "event":
                snaps.append(m)
        assert time.time() - t0 < 2.5
        assert "host" in snaps[0]
        services.inspector.event("genai_swap", from_="a", to="b")
        for _ in range(5):
            m = ws.receive_json()
            if m.get("kind") == "event":
                assert m["event"] == "genai_swap" and m["detail"]["from"] == "a"
                break
        else:
            pytest.fail("no event forwarded")


async def test_inspector_sse_yields_row(live_url):
    async with httpx.AsyncClient(base_url=live_url, timeout=5) as c:
        async with c.stream("GET", "/v1/inspector") as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            await asyncio.sleep(0.2)
            await c.get("/health")
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    assert '"/health"' in line
                    break


def test_inspector_recent_and_clients(client):
    client.get("/health")
    rows = client.get("/v1/inspector/recent?n=10").json()
    assert any(r.get("endpoint") == "/health" for r in rows["data"])
    cl = client.get("/v1/clients").json()["data"]
    assert cl and {"id", "addr", "user_agent", "last_seen", "requests"} <= set(cl[0])


def test_governor_switch(client, services):
    r = client.post("/v1/governor/genai", json={"model": "qwen2-vl"})
    assert r.status_code == 200
    assert services.governor.switches == ["qwen2-vl"]


def test_governor_switch_slot_busy(client, services):
    from aibox.services import SlotBusy

    async def boom(model):
        raise SlotBusy(3.0)

    services.governor.switch_genai = boom
    r = client.post("/v1/governor/genai", json={"model": "x"})
    assert r.status_code == 503 and r.headers["retry-after"] == "3"
    assert r.json()["error"]["code"] == "slot_busy"


def test_governor_switch_bad_body(client):
    assert client.post("/v1/governor/genai", json={}).status_code == 400
