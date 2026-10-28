"""Shared Hailo VDevice, async inference runners, device stats and the HEF page-cache warmer."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import numpy as np

from ..config import Settings
from ..inspector import Inspector
from .registry import Emitter, Registry


class InferRunner:
    """One configured InferModel. UINT8 NHWC in, FLOAT32 out; safe to call from many tasks."""

    def __init__(self, model_id: str, infer_model: Any, configured_ctx: Any, registry: Registry | None):
        self.model_id = model_id
        self._im = infer_model
        self._ctx = configured_ctx
        self._cim = configured_ctx.__enter__()
        self._registry = registry
        self._closed = False
        self.input_shape: tuple[int, ...] = tuple(infer_model.input().shape)
        self.output_names = [o.name for o in infer_model.outputs]
        self.last_infer_ms = 0.0
        self._gate = threading.Lock()
        self._inflight = 0

    def infer_sync(self, input_rgb: np.ndarray, timeout_ms: int = 10000) -> dict[str, Any]:
        with self._gate:
            if self._closed:
                raise RuntimeError(f"{self.model_id} is unloaded")
            self._inflight += 1
        try:
            return self._infer(input_rgb, timeout_ms)
        finally:
            with self._gate:
                self._inflight -= 1

    def _infer(self, input_rgb: np.ndarray, timeout_ms: int) -> dict[str, Any]:
        x = np.ascontiguousarray(input_rgb, dtype=np.uint8)
        if not x.flags.writeable:
            x = x.copy()
        if tuple(x.shape) != self.input_shape:
            raise ValueError(f"{self.model_id}: expected input {self.input_shape}, got {x.shape}")
        b = self._cim.create_bindings()
        b.input().set_buffer(x)
        for o in self._im.outputs:
            b.output(o.name).set_buffer(np.empty(o.shape, dtype=np.float32))
        t0 = time.perf_counter()
        self._cim.wait_for_async_ready(timeout_ms=timeout_ms)
        self._cim.run_async([b]).wait(timeout_ms)
        self.last_infer_ms = (time.perf_counter() - t0) * 1000.0
        if self._registry is not None:
            self._registry.record_call(self.model_id, self.last_infer_ms)
        return {n: b.output(n).get_buffer() for n in self.output_names}

    async def infer(self, input_rgb: np.ndarray) -> dict[str, Any]:
        return await asyncio.to_thread(self.infer_sync, input_rgb)

    def close(self) -> None:
        with self._gate:
            if self._closed:
                return
            self._closed = True  # new infers are refused from here on
        deadline = time.monotonic() + 5.0
        while self._inflight and time.monotonic() < deadline:
            time.sleep(0.005)
        ctx, self._ctx = self._ctx, None
        try:
            ctx.__exit__(None, None, None)  # deactivates the network group on the device
        finally:
            self._cim = None  # drop the ConfiguredInferModel so the device resources can be freed
            self._im = None


class NpuPool:
    def __init__(self, settings: Settings, inspector: Inspector | Emitter,
                 registry: Registry | None = None) -> None:
        self.settings = settings
        self.emit = inspector if isinstance(inspector, Emitter) else Emitter(inspector)
        self.registry = registry
        self._vd: Any = None
        self._lock = threading.Lock()
        self._runners: dict[str, InferRunner] = {}
        # cleared while the VDevice is being recycled; inference entry points wait on it
        self.ready = threading.Event()
        self.ready.set()

    async def wait_ready(self, timeout_s: float = 60.0) -> None:
        if not self.ready.is_set() and not await asyncio.to_thread(self.ready.wait, timeout_s):
            raise RuntimeError("NPU is being reconfigured; try again shortly")

    @property
    def vdevice(self) -> Any:
        with self._lock:
            if self._vd is None:
                from hailo_platform import VDevice

                p = VDevice.create_params()
                p.group_id = "SHARED"
                self._vd = VDevice(p)
            return self._vd

    def load(self, model_id: str, hef_path: str) -> InferRunner:
        from hailo_platform import FormatType

        vd = self.vdevice
        im = vd.create_infer_model(hef_path)
        im.input().set_format_type(FormatType.UINT8)
        for o in im.outputs:
            o.set_format_type(FormatType.FLOAT32)
        runner = InferRunner(model_id, im, im.configure(), self.registry)
        self._runners[model_id] = runner
        return runner

    def unload(self, model_id: str) -> None:
        r = self._runners.pop(model_id, None)
        if r is not None:
            r.close()

    def stats(self) -> dict:
        """NPU utilisation, device RAM and chip temperature; zeros when unavailable."""
        out = {"nnc_util_pct": 0.0, "ram_used_mb": 0.0, "ram_total_mb": 0.0, "temp_c": 0.0}
        try:
            dev = self.vdevice.get_physical_devices()[0]
        except Exception:  # noqa: BLE001
            return out
        for obj in (dev, getattr(dev, "control", None)):
            if obj is None:
                continue
            if hasattr(obj, "query_performance_stats"):
                try:
                    s = obj.query_performance_stats()
                    out["nnc_util_pct"] = max(0.0, float(s.nnc_utilization))
                    out["ram_used_mb"] = round(float(s.ram_size_used) / 1024.0, 1)
                    out["ram_total_mb"] = round(float(s.ram_size_total) / 1024.0, 1)
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(obj, "get_chip_temperature"):
                try:
                    t = obj.get_chip_temperature()
                    out["temp_c"] = round(max(float(t.ts0_temperature), float(t.ts1_temperature)), 1)
                except Exception:  # noqa: BLE001
                    pass
        return out

    def warm_cache(self, paths: list[str]) -> threading.Thread:
        """Read files into the page cache on a daemon thread (8 MB chunks)."""

        def run() -> None:
            t0 = time.monotonic()
            total = 0
            for p in paths:
                try:
                    with open(p, "rb", buffering=0) as f:
                        while chunk := f.read(8 * 1024 * 1024):
                            total += len(chunk)
                except OSError:
                    continue
            self.emit("warm_cache", files=len(paths), mb=round(total / 1e6, 1),
                      s=round(time.monotonic() - t0, 2))

        th = threading.Thread(target=run, name="hef-warm-cache", daemon=True)
        th.start()
        return th

    def close(self) -> None:
        for mid in list(self._runners):
            self.unload(mid)
        with self._lock:
            if self._vd is not None:
                try:
                    self._vd.release()
                except Exception:  # noqa: BLE001
                    pass
                self._vd = None
