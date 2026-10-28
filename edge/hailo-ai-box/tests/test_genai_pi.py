import os

import numpy as np
import pytest
from PIL import Image

from aibox.backends.npu import NpuPool
from aibox.backends.registry import Emitter, ModelSpec, Registry
from aibox.backends.vlm import build_genai
from aibox.config import Settings
from aibox.inspector import Inspector

pytestmark = pytest.mark.pi
DATA = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture
async def stack():
    ins = Inspector()
    emit = Emitter(ins)
    emit.bind_loop()
    st = Settings()
    reg = Registry(emit)
    for mid, mod, rt in [("qwen3:1.7b", "chat", "hailo-ollama"), ("qwen3-vl", "vlm", "hailort-genai")]:
        reg.register(ModelSpec(mid, mod, rt))
    pool = NpuPool(st, emit, reg)
    slot, vlm, ollama, chat = build_genai(emit, pool, st, reg)
    vlm.after_unload = ollama.after_unload = pool.close  # recycle the VDevice (returns GenAI RAM)
    yield ins, slot, vlm, ollama, chat, pool, reg
    await slot.release_all()
    await ollama.aclose()
    pool.close()


async def test_chat_vlm_chat_roundtrip(stack):
    ins, slot, vlm, ollama, chat, pool, reg = stack
    msgs = [{"role": "user", "content": "Say hello in five words."}]
    toks = [t async for t in chat.stream("qwen3:1.7b", msgs, 40)]
    assert len(toks) >= 5, toks
    img = np.array(Image.open(os.path.join(DATA, "sample_face.png")).convert("RGB"))
    ans = await vlm.ask(img, "What text is on the shirt?", "qwen3-vl", 64)
    print("VLM answer:", ans)
    assert "RACQ" in ans.upper()
    toks2 = [t async for t in chat.stream("qwen3:1.7b", msgs, 40)]
    assert len(toks2) >= 5
    swaps = [(e["detail"]["from"], e["detail"]["to"], e["detail"]["ms"])
             for e in ins.recent() if e.get("event") == "genai_swap"]
    print("swaps", swaps)
    assert any(s[1].startswith("vlm:") for s in swaps) and any(
        s[1].startswith("ollama:") for s in swaps[1:])
    print("npu", pool.stats())


async def test_metrics_and_cancel(stack):
    import time

    ins, slot, vlm, ollama, chat, pool, reg = stack
    msgs = [{"role": "user", "content": "Count from 1 to 40."}]
    agen = chat.stream("qwen3:1.7b", msgs, 200)  # first call also swaps (cold)
    t0 = time.perf_counter()
    n = 0
    async for _ in agen:
        n += 1
        if n == 1:
            t_first = time.perf_counter()
        if n == 15:
            break
    tps = (n - 1) / (time.perf_counter() - t_first)
    await agen.aclose()
    assert slot.state == "idle"
    print(f"ollama ttft_after_swap={t_first - t0:.2f}s tok/s={tps:.1f}")
    t1 = time.perf_counter()
    toks = [t async for t in chat.stream("qwen3:1.7b", [{"role": "user", "content": "hi"}], 6)]
    print(f"chat after cancel: {len(toks)} tokens, {time.perf_counter() - t1:.2f}s")
    img = np.array(Image.open(os.path.join(DATA, "c920e.png")).convert("RGB"))
    t2 = time.perf_counter()
    agen = vlm.stream("qwen3-vl", [{"role": "user", "content": "Describe the scene."}], 160, [img])
    m = 0
    async for _ in agen:
        m += 1
        if m == 1:
            print(f"vlm ttft_incl_swap={time.perf_counter() - t2:.2f}s")
        if m == 6:
            break
    await agen.aclose()
    assert slot.state == "idle"
    t3 = time.perf_counter()
    ans = await vlm.ask(img, "How many people are visible? Answer with a number.", "qwen3-vl", 8)
    print(f"vlm warm ask {time.perf_counter() - t3:.2f}s -> {ans!r}")
    print("registry", [(i["id"], i["state"], i["load_s"]) for i in reg.items()])
