import asyncio

import pytest

from aibox.backends.genai import GenAISlot
from aibox.inspector import Inspector
from aibox.services import SlotBusy


class Handle:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def unload(self):
        self.log.append(f"unload {self.name}")


def make(delay=0.0):
    log: list[str] = []
    ins = Inspector()

    def mk(name):
        async def loader():
            await asyncio.sleep(delay)
            log.append(f"load {name}")
            return Handle(log, name)
        return loader

    keys = ["vlm:qwen3-vl", "ollama:qwen3:1.7b", "ollama:llama3.2:1b"]
    return GenAISlot(ins, {k: mk(k) for k in keys}), ins, log


async def test_swap_order():
    slot, ins, log = make()
    async with slot.acquire("vlm:qwen3-vl"):
        assert slot.state == "generating"
    assert slot.state == "idle" and slot.owner == "vlm:qwen3-vl"
    log.clear()
    async with slot.acquire("ollama:qwen3:1.7b"):
        pass
    assert log == ["unload vlm:qwen3-vl", "load ollama:qwen3:1.7b"]
    swaps = [e for e in ins.recent() if e.get("event") == "genai_swap"]
    assert swaps[-1]["detail"]["from"] == "vlm:qwen3-vl"
    assert swaps[-1]["detail"]["to"] == "ollama:qwen3:1.7b" and "ms" in swaps[-1]["detail"]
    async with slot.acquire("ollama:qwen3:1.7b"):  # same owner: no swap
        pass
    assert len([e for e in ins.recent() if e.get("event") == "genai_swap"]) == 2


async def test_ninth_waiter_gets_slot_busy():
    slot, _, _ = make(delay=0.05)
    gate = asyncio.Event()

    async def hold():
        async with slot.acquire("vlm:qwen3-vl"):
            await gate.wait()

    async def wait_for(i):
        async with slot.acquire("ollama:qwen3:1.7b"):
            pass

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.2)
    waiters = [asyncio.create_task(wait_for(i)) for i in range(8)]
    await asyncio.sleep(0.05)
    assert slot.waiting() == 8
    with pytest.raises(SlotBusy) as ei:
        async with slot.acquire("ollama:qwen3:1.7b"):
            pass
    assert ei.value.retry_after_s > 0
    gate.set()
    await asyncio.wait_for(asyncio.gather(holder, *waiters), 5)
    assert slot.state == "idle" and slot.waiting() == 0


async def test_cancel_releases_generating_state():
    slot, _, _ = make()

    async def gen():
        async with slot.acquire("vlm:qwen3-vl"):
            await asyncio.sleep(10)

    t = asyncio.create_task(gen())
    await asyncio.sleep(0.05)
    assert slot.state == "generating"
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert slot.state == "idle"
    async with slot.acquire("vlm:qwen3-vl"):  # lock was released
        pass


async def test_cancel_while_waiting_does_not_leak_a_waiter():
    slot, _, _ = make()
    gate = asyncio.Event()

    async def hold():
        async with slot.acquire("vlm:qwen3-vl"):
            await gate.wait()

    h = asyncio.create_task(hold())
    await asyncio.sleep(0.02)
    w = asyncio.create_task(slot.acquire("ollama:qwen3:1.7b").__aenter__())
    await asyncio.sleep(0.02)
    assert slot.waiting() == 1
    w.cancel()
    await asyncio.gather(w, return_exceptions=True)
    assert slot.waiting() == 0
    gate.set()
    await h


async def test_failed_load_leaves_slot_empty_and_usable():
    ins = Inspector()
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Failed to acquire KV-Cache")
        return Handle([], "x")

    slot = GenAISlot(ins, {"vlm:qwen3-vl": bad})
    with pytest.raises(RuntimeError):
        async with slot.acquire("vlm:qwen3-vl"):
            pass
    assert slot.owner is None and slot.state == "idle"
    async with slot.acquire("vlm:qwen3-vl"):
        pass
    assert slot.owner == "vlm:qwen3-vl"


async def test_ollama_stream_cancel_and_tokens():
    import json

    import httpx

    from aibox.backends.ollama import OllamaChat

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            seen.setdefault("generate", []).append(json.loads(request.content))
            return httpx.Response(200, json={"done": True})
        body = json.loads(request.content)
        if not body["stream"]:
            seen["warm"] = body
            return httpx.Response(200, json={"done": True})
        seen["chat"] = body
        lines = [json.dumps({"message": {"content": t}, "done": False}) + "\n"
                 for t in ["a", "b", "c", "d"]]
        lines.append(json.dumps({"message": {"content": ""}, "done": True}) + "\n")
        return httpx.Response(200, content="".join(lines).encode())

    ins = Inspector()
    chat = OllamaChat.__new__(OllamaChat)
    chat.base_url, chat.registry, chat.pool, chat.log_path = "http://x", None, None, None
    chat.after_unload = None
    chat._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    chat.slot = GenAISlot(ins, lambda key: chat.loader(key.split(":", 1)[1]))
    got = [t async for t in chat.stream("qwen3:1.7b", [{"role": "user", "content": "hi"}], 9999)]
    assert got == ["a", "b", "c", "d"]
    assert seen["chat"]["keep_alive"] == 600 and seen["chat"]["options"]["num_predict"] == 512
    assert seen["warm"]["keep_alive"] == 600
    agen = chat.stream("qwen3:1.7b", [{"role": "user", "content": "hi"}], 8)
    assert await agen.__anext__() == "a"
    assert chat.slot.state == "generating"
    await agen.aclose()
    assert chat.slot.state == "idle"
    await chat.unload("qwen3:1.7b")
    assert seen["generate"][-1] == {"model": "qwen3:1.7b", "keep_alive": 0}


def test_vlm_prompt_and_fit():
    import numpy as np

    from aibox.backends.vlm import build_prompt, fit_frame

    p = build_prompt([{"role": "user", "content": "What text?"}], True)
    assert p[0]["role"] == "system"
    assert p[1]["content"] == [{"type": "image"}, {"type": "text", "text": "What text?"}]
    f = fit_frame(np.zeros((720, 1280, 3), np.uint8), (288, 512))
    assert f.shape == (288, 512, 3) and f.flags.writeable
    sq = fit_frame(np.full((720, 1280, 3), 255, np.uint8), (336, 336))
    assert sq.shape == (336, 336, 3) and sq[0, 0].sum() == 0 and sq[168, 168].sum() > 0


def test_ollama_hef_path_from_manifest(tmp_path, monkeypatch):
    import json

    from aibox.backends.ollama import ollama_hef_path

    manifests = tmp_path / "manifests"
    model_dir = manifests / "qwen2.5" / "1.5b"
    model_dir.mkdir(parents=True)
    digest = "abc123"
    (model_dir / "manifest.json").write_text(json.dumps({"hef_h10h": digest}))
    data_home = tmp_path / "data"
    blob_dir = data_home / "hailo-ollama" / "models" / "blob"
    blob_dir.mkdir(parents=True)
    hef = blob_dir / f"sha256_{digest}"
    hef.write_bytes(b"hef")
    monkeypatch.setenv("HAILO_OLLAMA_MANIFESTS", str(manifests))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    assert ollama_hef_path("qwen2.5:1.5b") == str(hef)
    assert ollama_hef_path("qwen3:1.7b") is None
    assert ollama_hef_path("invalid") is None


async def test_ollama_unload_reclaims_exactly_once_with_high_reported_free_ram():
    import httpx

    from aibox.backends.ollama import OllamaChat

    calls = []
    chat = OllamaChat.__new__(OllamaChat)
    chat.registry = None
    chat.pool = type("Pool", (), {"stats": lambda self: {
        "ram_total_mb": 7000.0, "ram_used_mb": 0.0}})()
    chat.after_unload = lambda: calls.append("recycle")
    chat.slot = type("Slot", (), {"emit": staticmethod(lambda *a, **k: None)})()
    chat._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"done": True})))
    await chat.unload("qwen2.5:1.5b")
    assert calls == ["recycle"]
    await chat._client.aclose()


async def test_vlm_worker_start_failure_evicts_recycles_and_retries():
    from aibox.backends.vlm import HailoVlm
    from aibox.config import Settings

    calls = []
    vlm = HailoVlm(None, None, Settings(fake=True), registry=None)

    def spawn(model):
        calls.append("spawn")
        if len(calls) == 1:
            raise RuntimeError("VLM worker did not start")
        return object()

    vlm._spawn = spawn
    vlm.on_resource_exhausted = lambda: calls.append("evict") or True
    vlm.after_unload = lambda: calls.append("recycle")
    handle = await vlm.loader("qwen3-vl")()
    assert handle is not None
    assert calls == ["spawn", "evict", "recycle", "spawn"]
    await vlm.aclose()
