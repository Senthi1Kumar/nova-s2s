import asyncio
import io
import os
import time
import wave

import numpy as np
import pytest

from aibox.backends import RESIDENT_VISION, build_services
from aibox.config import Settings
from aibox.media import decode_audio

pytestmark = pytest.mark.pi
WAV = os.path.expanduser(
    "~/repos/test_nova_repo/nova-s2s/edge/nova-hailo/bench/corpus/speech_v1/audio/"
    "sp_019__default__ch1.wav")


@pytest.fixture(scope="module")
def svc():
    t0 = time.perf_counter()
    s = build_services(Settings())
    print(f"build_services took {time.perf_counter() - t0:.1f}s")
    yield s
    s.vision.close()
    s.asr.close()
    s.vision.pool.close()


def test_resident_set_and_vlm_in_slot(svc):
    items = {i["id"]: i for i in svc.governor.models()}
    for mid in RESIDENT_VISION + ["whisper-base", "piper-amy"]:
        assert items[mid]["state"] == "resident", items[mid]
    assert items["qwen3-vl"]["state"] == "resident"
    assert svc.governor.genai_owner() == "vlm:qwen3-vl"
    print("residency", [(i["id"], i["state"], i["load_s"]) for i in items.values()])
    print("npu", svc.governor.npu_stats())


async def test_whisper(svc):
    pcm = decode_audio(open(WAV, "rb").read()) * 3.0  # XVF3800 ch1 needs gain x3
    pcm = np.clip(pcm, -1, 1)
    t0 = time.perf_counter()
    text = await svc.asr.transcribe(pcm)
    print(f"whisper {len(pcm) / 16000:.1f}s audio -> {time.perf_counter() - t0:.2f}s: {text!r}")
    assert "rock" in text.lower()


async def test_piper(svc):
    t0 = time.perf_counter()
    wav = await svc.tts.synthesize("Hello from the AI box")
    with wave.open(io.BytesIO(wav)) as w:
        dur = w.getnframes() / w.getframerate()
    print(f"piper {dur:.2f}s audio in {time.perf_counter() - t0:.2f}s")
    assert dur > 0.5


async def test_detection_and_chat_through_services(svc):
    from PIL import Image

    img = np.array(Image.open(os.path.join(os.path.dirname(__file__), "data", "c920e.png"))
                   .convert("RGB"))
    res = await svc.vision.detect(img, "detection")
    assert any(d["class"] == "person" for d in res["detections"])
    toks = [t async for t in svc.chat.stream("qwen3:1.7b",
                                             [{"role": "user", "content": "Say hi."}], 12)]
    assert toks
    assert svc.governor.genai_owner() == "ollama:qwen3:1.7b"
    await asyncio.sleep(0)
