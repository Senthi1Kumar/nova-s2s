"""Real NPU / CPU backends for the Hailo AI Box."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import time

from ..config import Settings
from ..inspector import Inspector
from ..services import Services

log = logging.getLogger("aibox.backends")

CHAT_MODELS = ["qwen3:1.7b", "qwen2.5:1.5b", "llama3.2:1b", "qwen2:1.5b"]
# ufld_v2_tu (lanes) is loaded on demand: Whisper + 7 vision HEFs + a VLM exceed the chip's
# resources (HAILO_RESOURCE_EXHAUSTED), and the VLM is part of the resident contract.
RESIDENT_VISION = ["yolov8m", "yolov8m_pose", "yolov5m_seg", "scrfd_10g", "ocr_det", "ocr"]


def build_services(settings: Settings) -> Services:
    """Create every real backend, load the resident set, never raise on one model's failure."""
    from .asr import HailoWhisper
    from .cameras import Cameras
    from .governor import HailoGovernor, pi_temp_c
    from .npu import NpuPool
    from .registry import Emitter, ModelSpec, Registry
    from .tts import PiperTts
    from .vision import HailoVision
    from .vlm import VLM_MODELS, build_genai, vlm_hef

    inspector = Inspector()
    emit = Emitter(inspector)
    registry = Registry(emit)
    pool = NpuPool(settings, emit, registry)

    for m in CHAT_MODELS:
        registry.register(ModelSpec(m, "chat", "hailo-ollama"))
    for m in VLM_MODELS:
        path = vlm_hef(settings, m)
        size = round(os.path.getsize(path) / 1e6, 1) if os.path.exists(path) else None
        registry.register(ModelSpec(m, "vlm", "hailort-genai", size_mb=size, hef=path))
    registry.register(ModelSpec("whisper-base", "asr", "hailort"))
    registry.register(ModelSpec("piper-amy", "tts", "piper", device="cpu"))

    vision = HailoVision(pool, registry)
    asr = HailoWhisper(pool, registry)
    tts = PiperTts(settings.piper_voice, registry)
    slot, vlm, ollama, chat = build_genai(emit, pool, settings, registry)
    vlm.on_resource_exhausted = vision.evict_for_genai

    async def drop_genai() -> str | None:
        owner = slot.owner
        # Each owner's unload hook reclaims its device RAM when needed. Recycling here as well
        # closes and reloads every vision model twice and can wedge HailoRT in an async-job wait.
        await slot.release_all()
        return owner

    vision.release_genai = drop_genai
    ollama.on_resource_exhausted = vision.evict_one

    def recycle_npu() -> None:
        """Drop and recreate this process's VDevice: the only way to return a VLM's device RAM."""
        t0 = time.monotonic()
        pool.ready.clear()
        try:
            ids = [i for i in RESIDENT_VISION if i in vision.runners] + [
                m for m in vision.runners if m not in RESIDENT_VISION]
            had_asr = asr._s2t is not None
            vision.close()
            asr.close()
            pool.close()
            vision.load_resident(ids)
            if had_asr:
                asr.load()
        finally:
            pool.ready.set()
        emit("npu_recycle", ms=round((time.monotonic() - t0) * 1000), models=len(ids))

    vlm.after_unload = recycle_npu
    ollama.after_unload = recycle_npu
    governor = HailoGovernor(registry, slot, pool, pi_temp_c, emit)
    cameras = Cameras(settings)

    from .ollama import ollama_hef_path

    # Warm the active VLM before loading it. Then pre-read only the default alternate chat HEF;
    # caching every multi-GB model would evict useful pages on an 8 GB Pi.
    pool.warm_cache([settings.qwen3_vl_hef]).join()
    warm_paths = vision.hef_paths() + [asr.hef]
    alternate_hef = ollama_hef_path("qwen2.5:1.5b")
    if alternate_hef:
        warm_paths.append(alternate_hef)
    pool.warm_cache(warm_paths)

    def guarded(name: str, fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("startup: %s failed", name)

    guarded("tts", tts.load)
    guarded("asr", asr.load)
    guarded("vision", lambda: vision.load_resident(RESIDENT_VISION))

    async def load_vlm() -> None:
        try:
            await governor.switch_genai("qwen3-vl")
        except Exception:  # noqa: BLE001 - recorded in the registry + inspector
            log.exception("startup: qwen3-vl failed to load")

    asyncio.run(load_vlm())
    emit.unbind()
    def shutdown() -> None:
        # Process teardown releases the device itself; do not schedule another recycle from atexit.
        vlm.after_unload = None
        ollama.after_unload = None
        ollama.pool = None
        try:
            asyncio.run(slot.release_all())
        except Exception:  # noqa: BLE001
            log.exception("shutdown: releasing the GenAI slot failed")
        for fn in (vision.close, asr.close, pool.close):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("shutdown: %s failed", getattr(fn, "__qualname__", fn))

    atexit.register(shutdown)

    from .realsense_id import RealSenseFaceAuth

    face_auth = None
    registry.register(ModelSpec("realsense-id", "face_auth", "realsense-id",
                                task="authentication", device="f455"))
    try:
        face_auth = RealSenseFaceAuth(settings.realsense_lib)
        registry.set_state("realsense-id", "resident")
    except Exception as exc:  # noqa: BLE001 - optional F455 must not block other services
        log.warning("RealSense ID unavailable: %s", exc)
        registry.set_state("realsense-id", "failed", error=str(exc))

    return Services(vision=vision, vlm=vlm, chat=chat, asr=asr, tts=tts, cameras=cameras,
                    governor=governor, inspector=inspector, settings=settings,
                    face_auth=face_auth)
