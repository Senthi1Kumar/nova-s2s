"""Lanes eviction: LRU order, pinned set, retry, events, clean release and reload."""

import asyncio

import pytest

from aibox.backends import vision as V
from aibox.backends.vision import HailoVision
from aibox.config import Settings
from aibox.inspector import Inspector
from aibox.backends.registry import Registry


class FakeRunner:
    def __init__(self, mid):
        self.model_id, self.closed = mid, False

    def close(self):
        self.closed = True


class FakePool:
    """Holds at most `capacity` models; loading past it raises HAILO_RESOURCE_EXHAUSTED."""

    def __init__(self, capacity, tmp):
        self.settings = Settings(fake=True, hef_dir=str(tmp))
        self.capacity, self.runners, self.loads = capacity, {}, []

    def load(self, mid, path):
        if len(self.runners) >= self.capacity:
            raise RuntimeError("libhailort failed with error: 81 (HAILO_RESOURCE_EXHAUSTED)")
        self.loads.append(mid)
        self.runners[mid] = FakeRunner(mid)
        return self.runners[mid]

    def unload(self, mid):
        self.runners.pop(mid).close()


@pytest.fixture
def box(tmp_path):
    for vm in V.VISION_MODELS:
        (tmp_path / vm.hef).write_bytes(b"x")
    ins = Inspector()
    reg = Registry(ins)
    pool = FakePool(6, tmp_path)
    return HailoVision(pool, reg), pool, reg, ins


def _load(v, ids):
    v.load_resident(ids)


def _events(ins, name):
    return [e["detail"] for e in ins.recent() if e.get("event") == name]


def test_evicts_lru_evictable_then_retries(box):
    v, pool, reg, ins = box
    _load(v, ["yolov8m", "scrfd_10g", "yolov8m_pose", "yolov5m_seg", "ocr_det", "ocr"])
    reg.record_call("yolov5m_seg", 5)
    reg.record_call("ocr", 5)
    reg.record_call("ocr_det", 5)
    # yolov8m_pose never used (oldest) -> evicted first
    asyncio.run(v.ensure(["ufld_v2_tu"]))
    ev = _events(ins, "model_evict")
    assert [e["model"] for e in ev] == ["yolov8m_pose"]
    assert ev[0]["for"] == "ufld_v2_tu" and ev[0]["reason"] == "resource_exhausted"
    assert "ufld_v2_tu" in v.runners and "yolov8m_pose" not in v.runners
    states = [(e["model"], e["state"]) for e in _events(ins, "model_state")]
    assert ("yolov8m_pose", "evicting") in states and ("yolov8m_pose", "unloaded") in states
    assert ("ufld_v2_tu", "resident") in states


def test_pinned_never_evicted_and_ocr_pair_goes_together(box):
    v, pool, reg, ins = box
    pool.capacity = 4
    _load(v, ["yolov8m", "scrfd_10g", "ocr_det", "ocr"])
    asyncio.run(v.ensure(["ufld_v2_tu", "yolov8m_pose"]))
    assert {"yolov8m", "scrfd_10g"} <= set(v.runners)
    assert {m["model"] for m in _events(ins, "model_evict")} == {"ocr_det", "ocr"}
    assert pool.runners.keys() == v.runners.keys()


def test_nothing_evictable_raises(box):
    v, pool, reg, ins = box
    pool.capacity = 2
    _load(v, ["yolov8m", "scrfd_10g"])
    with pytest.raises(RuntimeError, match="no free NPU resources"):
        asyncio.run(v.ensure(["ufld_v2_tu"]))
    assert set(v.runners) == {"yolov8m", "scrfd_10g"}
    assert not _events(ins, "model_evict")


def test_victim_released_and_reloads_on_next_use(box):
    v, pool, reg, ins = box
    pool.capacity = 3
    _load(v, ["yolov8m", "scrfd_10g", "yolov5m_seg"])
    seg = pool.runners["yolov5m_seg"]
    asyncio.run(v.ensure(["ufld_v2_tu"]))
    assert seg.closed and reg.state("yolov5m_seg") == "unloaded"
    asyncio.run(v.ensure(["yolov5m_seg"]))  # evicts ufld (LRU evictable), seg back
    assert "yolov5m_seg" in v.runners and "ufld_v2_tu" not in v.runners
    assert reg.state("ufld_v2_tu") == "unloaded"
    assert pool.loads.count("yolov5m_seg") == 2


def test_non_exhaustion_error_not_retried(box):
    v, pool, reg, ins = box

    def boom(mid, path):
        raise RuntimeError("HAILO_INVALID_HEF(26)")

    pool.load = boom
    with pytest.raises(RuntimeError, match="failed to load"):
        asyncio.run(v.ensure(["ufld_v2_tu"]))


def test_genai_released_as_last_resort_once(box):
    v, pool, reg, ins = box
    pool.capacity = 2
    _load(v, ["yolov8m", "scrfd_10g"])
    calls = []

    async def drop():
        calls.append(1)
        pool.capacity = 3  # the VLM's device RAM comes back
        return "qwen3-vl"

    v.release_genai = drop
    asyncio.run(v.ensure(["ufld_v2_tu"]))
    assert calls == [1] and "ufld_v2_tu" in v.runners
    assert [e["model"] for e in _events(ins, "model_evict")] == ["qwen3-vl"]


def test_ollama_load_evicts_vision_until_it_fits(box):
    import httpx

    from aibox.backends.ollama import OllamaChat

    v, pool, reg, ins = box
    pool.capacity = 6
    _load(v, ["yolov8m", "scrfd_10g", "yolov8m_pose", "yolov5m_seg", "ocr_det", "ocr"])
    reg.register(__import__("aibox.backends.registry", fromlist=["ModelSpec"]).ModelSpec(
        "qwen3:1.7b", "chat", "hailo-ollama"))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:  # fails until two evictions happened
            return httpx.Response(500, text="Failed to create LLM")
        return httpx.Response(200, json={"message": {"content": "hi"}, "done": True})

    class Slot:
        emit = staticmethod(lambda *a, **k: None)

    oc = OllamaChat(Slot(), "http://x", reg)
    oc._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    oc.on_resource_exhausted = v.evict_one
    asyncio.run(oc.loader("qwen3:1.7b")())
    ev = [e["model"] for e in _events(ins, "model_evict")]
    assert len(ev) == 2 and set(ev) <= V.EVICTABLE and not {"yolov8m", "scrfd_10g"} & set(ev)
    assert reg.state("qwen3:1.7b") == "resident"


def test_ollama_load_fails_clearly_when_nothing_to_evict(box):
    import httpx

    from aibox.backends.ollama import OllamaChat

    v, pool, reg, ins = box
    _load(v, ["yolov8m", "scrfd_10g"])
    reg.register(__import__("aibox.backends.registry", fromlist=["ModelSpec"]).ModelSpec(
        "qwen3:1.7b", "chat", "hailo-ollama"))

    class Slot:
        emit = staticmethod(lambda *a, **k: None)

    oc = OllamaChat(Slot(), "http://x", reg)
    oc._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(
        lambda r: httpx.Response(500, text="Failed to create LLM")))
    oc.on_resource_exhausted = v.evict_one
    with pytest.raises(RuntimeError, match="no free NPU resources"):
        asyncio.run(oc.loader("qwen3:1.7b")())
    assert set(v.runners) == {"yolov8m", "scrfd_10g"}
