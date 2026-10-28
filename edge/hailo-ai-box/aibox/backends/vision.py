"""HailoRT inference for CV tasks and smoothed SCRFD face tracking."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from ..services import VisionService
from .npu import InferRunner, NpuPool
from .post import detection as det
from .post import dms, lanes, ocr, pose, segmentation
from .registry import ModelSpec, Registry

TASK_ORDER = ["detection", "pose", "segmentation", "lanes", "dms", "ocr", "face_track"]


@dataclass(frozen=True)
class VisionModel:
    id: str
    hef: str
    task: str


VISION_MODELS = [
    VisionModel("yolov8m", "yolov8m.hef", "detection"),
    VisionModel("yolov8m_pose", "yolov8m_pose.hef", "pose"),
    VisionModel("yolov5m_seg", "yolov5m_seg.hef", "segmentation"),
    VisionModel("ufld_v2_tu", "ufld_v2_tu.hef", "lanes"),
    VisionModel("scrfd_10g", "scrfd_10g.hef", "dms"),
    VisionModel("ocr_det", "ocr_det.hef", "ocr"),
    VisionModel("ocr", "ocr.hef", "ocr"),
]
TASK_MODELS = {"detection": ["yolov8m"], "pose": ["yolov8m_pose"], "segmentation": ["yolov5m_seg"],
               "lanes": ["ufld_v2_tu"], "dms": ["scrfd_10g", "yolov8m_pose", "yolov8m"],
               "ocr": ["ocr_det", "ocr"], "face_track": ["scrfd_10g"]}
# The chip cannot hold every vision model + Whisper + a VLM at once (HAILO_RESOURCE_EXHAUSTED);
# these load on demand and are the first to be evicted.
LAZY_MODELS = ("ufld_v2_tu",)
# Evictable under pressure, in groups released together (the OCR pair is one unit). Never evicted:
# yolov8m, scrfd_10g (the pinned detection/DMS base), Whisper and the GenAI slot.
EVICTABLE_GROUPS = (("yolov8m_pose",), ("yolov5m_seg",), ("ocr_det", "ocr"), ("ufld_v2_tu",))
EVICTABLE = frozenset(m for g in EVICTABLE_GROUPS for m in g)
TASK_MODEL = {"detection": "yolov8m", "pose": "yolov8m_pose", "segmentation": "yolov5m_seg",
              "lanes": "ufld_v2_tu", "dms": "scrfd_10g", "ocr": "ocr_det", "face_track": "scrfd_10g"}


class HailoVision(VisionService):
    def __init__(self, pool: NpuPool, registry: Registry) -> None:
        self.pool = pool
        self.registry = registry
        self.runners: dict[str, InferRunner] = {}
        # task -> async handler(self, image, conf, model) -> dict (without the common fields)
        self._handlers: dict[str, Callable] = {
            "detection": self._detection,
            "pose": self._pose,
            "segmentation": self._segmentation,
            "lanes": self._lanes,
            "dms": self._dms,
            "ocr": self._ocr,
            "face_track": self._face_track,
        }
        self.hef_dir = pool.settings.hef_dir
        self._ensure_lock = asyncio.Lock()
        # last resort when every evictable vision model is gone and the chip is still full: the
        # VLM's device RAM is what blocks lanes. Returns the released owner key (or None); the
        # GenAI slot reloads on its next request.
        self.release_genai: Callable[[], Awaitable[str | None]] | None = None
        for vm in VISION_MODELS:
            path = os.path.join(self.hef_dir, vm.hef)
            size = round(os.path.getsize(path) / 1e6, 1) if os.path.exists(path) else None
            registry.register(ModelSpec(vm.id, "vision", "hailort", task=vm.task,
                                        size_mb=size, hef=path))

    # -- lifecycle ---------------------------------------------------------------------------
    def hef_paths(self) -> list[str]:
        return [os.path.join(self.hef_dir, vm.hef) for vm in VISION_MODELS]

    def load_model(self, model_id: str) -> InferRunner | None:
        vm = next(v for v in VISION_MODELS if v.id == model_id)
        path = os.path.join(self.hef_dir, vm.hef)

        def loader() -> InferRunner:
            if not os.path.exists(path):
                raise FileNotFoundError(f"HEF not found: {path}")
            return self.pool.load(model_id, path)

        r = self.registry.load(model_id, loader)
        if r is not None:
            self.runners[model_id] = r
        return r

    def load_resident(self, ids: list[str] | None = None) -> None:
        for vm in VISION_MODELS:
            if ids is None or vm.id in ids:
                self.load_model(vm.id)

    def unload_model(self, model_id: str) -> None:
        self.registry.set_state(model_id, "evicting")
        self.runners.pop(model_id, None)
        self.pool.unload(model_id)
        self.registry.set_state(model_id, "unloaded")

    def evict_for_genai(self, for_model: str = "qwen3-vl") -> bool:
        """Sync (thread) hook for a failing VLM load: evict the LRU evictable group, if any."""
        victim = self._lru_victim(set())
        if victim is None:
            return False
        self.evict(victim, "resource_exhausted", for_model)
        return True

    def evict_lazy(self) -> bool:
        """Free the on-demand models (used when a VLM load hits RESOURCE_EXHAUSTED)."""
        gone = [m for m in LAZY_MODELS if m in self.runners]
        for m in gone:
            self.unload_model(m)
        return bool(gone)

    def close(self) -> None:
        for mid in list(self.runners):
            self.unload_model(mid)

    def _lru_victim(self, protect: set[str]) -> list[str] | None:
        """The resident evictable group (as model ids) whose newest use is oldest, else None."""
        last = {i["id"]: i["last_used"] or 0.0 for i in self.registry.items()}
        cands = []
        for group in EVICTABLE_GROUPS:
            held = [m for m in group if m in self.runners]
            if held and not protect.intersection(group):
                cands.append((max(last.get(m, 0.0) for m in held), held))
        cands.sort(key=lambda c: c[0])
        return cands[0][1] if cands else None

    async def evict_one(self, for_model: str) -> bool:
        """Evict the LRU evictable group to make room for `for_model` (a GenAI load)."""
        async with self._ensure_lock:
            victim = self._lru_victim(set())
            if victim is None:
                return False
            await asyncio.to_thread(self.evict, victim, "resource_exhausted", for_model)
            return True

    def evict(self, models: list[str], reason: str, for_model: str) -> None:
        for m in models:
            self.registry.emit("model_evict", model=m, reason=reason, **{"for": for_model})
            self.unload_model(m)

    async def ensure(self, models: list[str]) -> None:
        """Make `models` resident, evicting least-recently-used evictable vision models when the
        chip is full (HAILO_RESOURCE_EXHAUSTED) and retrying. Evicted models reload on next use."""
        need = [m for m in models if m not in self.runners]
        if not need:
            return
        async with self._ensure_lock:
            for mid in need:
                if mid in self.runners:
                    continue
                released = False
                while True:
                    if await asyncio.to_thread(self.load_model, mid) is not None:
                        break
                    err = next((i["error"] for i in self.registry.items() if i["id"] == mid), "") or ""
                    if "RESOURCE_EXHAUSTED" not in err and "(81)" not in err and " 81 " not in err:
                        raise RuntimeError(f"model {mid} failed to load: {err}")
                    victim = self._lru_victim(set(models))
                    if victim is None and not released and self.release_genai is not None:
                        released = True
                        owner = await self.release_genai()
                        if owner:
                            self.registry.emit("model_evict", model=owner, reason="resource_exhausted",
                                               **{"for": mid})
                            continue
                    if victim is None:
                        raise RuntimeError(f"model {mid}: no free NPU resources ({err})")
                    await asyncio.to_thread(self.evict, victim, "resource_exhausted", mid)

    # -- VisionService -----------------------------------------------------------------------
    def tasks(self) -> list[str]:
        return [t for t in TASK_ORDER if t in self._handlers]

    def missing_models(self, task: str, model: str | None = None) -> list[str]:
        """Models `task` needs that are not resident right now (loading, evicted or unloaded)."""
        need = TASK_MODELS.get(task, []) if model is None else [model]
        return [m for m in need if m not in self.runners]

    async def detect(self, image: np.ndarray, task: str, conf: float = 0.35,
                     model: str | None = None) -> dict:
        handler = self._handlers.get(task)
        if handler is None:
            raise ValueError(f"unknown vision task {task!r}")
        mid = model or TASK_MODEL[task]
        await self.pool.wait_ready()
        await self.ensure(TASK_MODELS[task] if model is None else [mid])
        h, w = image.shape[:2]
        t0 = time.perf_counter()
        out = await handler(image, conf, mid)
        ms = (time.perf_counter() - t0) * 1000.0
        return {"model": mid, "task": task, "inference_ms": round(ms, 1),
                "image_width": w, "image_height": h, **out}

    async def _detection(self, image: np.ndarray, conf: float, model: str) -> dict:
        runner = self.runners[model]
        h, w = image.shape[:2]
        th, tw = runner.input_shape[:2]
        canvas, s, px, py = det.letterbox(image, (th, tw))
        raw = await runner.infer(canvas)
        per_class = next(iter(raw.values()))
        return {"detections": det.parse_nms_by_class(per_class, (th, tw), s, px, py, w, h, conf)}

    async def _pose(self, image: np.ndarray, conf: float, model: str) -> dict:
        runner = self.runners[model]
        h, w = image.shape[:2]
        th, tw = runner.input_shape[:2]
        canvas, s, px, py = det.letterbox(image, (th, tw))
        raw = await runner.infer(canvas)
        return {"detections": pose.parse(raw, (th, tw), s, px, py, w, h, conf)}

    async def _segmentation(self, image: np.ndarray, conf: float, model: str) -> dict:
        runner = self.runners[model]
        h, w = image.shape[:2]
        th, tw = runner.input_shape[:2]
        canvas, s, px, py = det.letterbox(image, (th, tw))
        raw = await runner.infer(canvas)
        return {"detections": segmentation.parse(raw, (th, tw), s, px, py, w, h, conf)}

    async def _lanes(self, image: np.ndarray, conf: float, model: str) -> dict:
        runner = self.runners[model]
        h, w = image.shape[:2]
        canvas = lanes.preprocess(image, runner.input_shape[:2])
        raw = await runner.infer(canvas)
        return {"lanes": lanes.parse(raw, w, h)}

    async def _dms(self, image: np.ndarray, conf: float, model: str) -> dict:
        runner = self.runners[model]
        h, w = image.shape[:2]
        th, tw = runner.input_shape[:2]
        canvas, s, px, py = det.letterbox(image, (th, tw))
        raw = await runner.infer(canvas)
        faces = dms.select_driver(dms.parse_faces(raw, (th, tw), s, px, py, w, h, conf), w, h)
        objects = (await self._detection(image, conf, "yolov8m"))["detections"]
        poses = (await self._pose(image, conf, "yolov8m_pose"))["detections"]
        return {"detections": faces, "dms": dms.assess(faces, objects, poses, w, h)}

    async def _face_track(self, image: np.ndarray, conf: float, model: str) -> dict:
        """SCRFD only (no object/pose pass): the driver face box + 5 landmarks for the Face ID overlay."""
        runner = self.runners[model]
        h, w = image.shape[:2]
        th, tw = runner.input_shape[:2]
        canvas, s, px, py = det.letterbox(image, (th, tw))
        raw = await runner.infer(canvas)
        return {"detections": dms.select_driver(dms.parse_faces(raw, (th, tw), s, px, py, w, h, conf), w, h)}

    async def _ocr(self, image: np.ndarray, conf: float, model: str) -> dict:
        detector = self.runners[model]
        recognizer = self.runners["ocr"]
        th, tw = detector.input_shape[:2]
        canvas, _, _, _ = det.letterbox(image, (th, tw))
        raw = await detector.infer(canvas)
        regions = ocr.detect_regions(next(iter(raw.values())), image, (th, tw))
        lines = []
        for region in regions:
            crop = np.ascontiguousarray(region["crop"][..., ::-1])
            prepared = ocr.resize_recognition(crop, recognizer.input_shape[:2])
            recognition = await recognizer.infer(prepared)
            text, score = ocr.decode_text(next(iter(recognition.values())))
            if text.strip():
                lines.append({"text": text, "bbox": region["bbox"],
                              "confidence": round(score, 4)})
        return {"text": lines}
