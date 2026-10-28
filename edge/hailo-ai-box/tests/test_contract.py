import asyncio

import numpy as np

from aibox import services as S
from aibox.config import load_settings
from aibox.inspector import Inspector


def test_fake_services_satisfy_protocols(services):
    assert isinstance(services.vision, S.VisionService)
    assert isinstance(services.vlm, S.VlmService)
    assert isinstance(services.chat, S.ChatService)
    assert isinstance(services.asr, S.AsrService)
    assert isinstance(services.tts, S.TtsService)
    assert isinstance(services.cameras, S.CameraService)
    assert isinstance(services.governor, S.Governor)


async def test_fake_detect_shape(services):
    img = np.zeros((360, 640, 3), np.uint8)
    r = await services.vision.detect(img, "detection")
    assert {"model", "task", "inference_ms", "image_width", "image_height",
            "detections"} <= set(r)
    assert r["detections"][0]["bbox"] == [10, 20, 110, 220]
    assert (r["image_width"], r["image_height"]) == (640, 360)


async def test_inspector_ring_and_subscribe():
    insp = Inspector()
    for i in range(2001):
        insp.record({"i": i})
    assert len(insp.recent(5000)) == 2000
    sub = insp.subscribe()
    insp.event("genai_swap", from_="a", to="b")
    e = await asyncio.wait_for(anext(sub), 1)
    assert e["kind"] == "event" and e["event"] == "genai_swap"
    assert e["detail"] == {"from": "a", "to": "b"}
    await sub.aclose()


def test_inspector_clients():
    insp = Inspector()
    insp.touch_client("c1", "1.2.3.4", "ua")
    insp.touch_client("c1", "1.2.3.4", "ua")
    (c,) = insp.clients()
    assert c["id"] == "c1" and c["requests"] == 2 and c["user_agent"] == "ua"


async def test_fake_chat_tokens_and_cancel(services):
    got = [t async for t in services.chat.stream("m", [], 8)]
    assert got == ["Hello", " from", " the", " box"]
    assert services.chat.cancelled is False
    gen = services.chat.stream("m", [], 8)
    await anext(gen)
    await gen.aclose()
    assert services.chat.cancelled is True


async def test_fake_asr_tts_camera_vlm(services):
    import io
    import wave

    assert await services.asr.transcribe(np.zeros(16000, np.float32)) == "hello box"
    with wave.open(io.BytesIO(await services.tts.synthesize("hi"))) as w:
        assert w.getnframes() / w.getframerate() == 0.2
    gen = services.cameras.frames("c920e", 50)
    f = await anext(gen)
    await gen.aclose()
    assert f.shape == (360, 640, 3) and f.dtype == np.uint8
    await services.vlm.ask(f, "what?")
    assert services.vlm.calls == 1


def test_fake_governor_and_models(services):
    g = services.governor
    ids = {m["id"] for m in g.models()}
    assert {"qwen3:1.7b", "qwen2.5:1.5b", "llama3.2:1b", "qwen2:1.5b", "qwen3-vl", "qwen2-vl",
            "whisper-base", "piper-amy", "yolov8m", "yolov8m_pose", "yolov5m_seg", "ufld_v2_tu",
            "scrfd_10g", "ocr_det", "ocr"} <= ids
    assert all(m["state"] == "resident" for m in g.models())
    assert g.genai_state() == "idle" and g.vision_fps_cap() is None
    assert {"nnc_util_pct", "ram_used_mb", "ram_total_mb", "temp_c"} <= set(g.npu_stats())


def test_slotbusy():
    assert S.SlotBusy(3.0).retry_after_s == 3.0


def test_settings_env():
    s = load_settings({"AIBOX_HTTPS_PORT": "9443", "AIBOX_FAKE": "1", "AIBOX_API_KEY": "k",
                       "AIBOX_CAMERA_C920E": "/dev/v4l/by-id/c920e-test",
                       "AIBOX_CAMERA_F455": "/dev/v4l/by-id/f455-test"})
    assert (s.https_port, s.http_port, s.fake, s.api_key) == (9443, 8442, True, "k")
    assert s.cameras == {"c920e": "/dev/v4l/by-id/c920e-test",
                         "f455": "/dev/v4l/by-id/f455-test"}
    d = load_settings({})
    assert (d.https_port, d.fake, d.api_key) == (8443, False, None)
    assert set(d.cameras) == {"c920e", "f455"}
