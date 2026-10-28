"""Governor facade: residency, GenAI slot, NPU stats and thermal steps for vision fps."""

from __future__ import annotations

import time
from collections.abc import Callable

from ..services import Governor
from .genai import GenAISlot
from .npu import NpuPool
from .registry import Emitter, Registry
from .vlm import VLM_MODELS

THERMAL_STEPS = ((83.0, 2.0), (80.0, 5.0))  # (temp_c >=, fps cap)
THERMAL_RECOVER_C = 76.0


class HailoGovernor(Governor):
    def __init__(self, registry: Registry, slot: GenAISlot, pool: NpuPool,
                 telemetry_temp_fn: Callable[[], float | None], emit: Emitter | None = None,
                 poll_s: float = 1.0) -> None:
        self.registry, self.slot, self.pool = registry, slot, pool
        self._temp_fn = telemetry_temp_fn
        self.emit = emit or registry.emit
        self._poll_s = poll_s
        self._polled = 0.0
        self._cap: float | None = None

    # -- thermal ----------------------------------------------------------------------------
    def observe_temp(self, temp_c: float) -> float | None:
        """Feed one Pi temperature sample (1 Hz); returns the current vision fps cap."""
        new = self._cap
        for threshold, cap in THERMAL_STEPS:
            if temp_c >= threshold:
                # never loosen while still hot: a step only tightens until the recovery point
                new = cap if self._cap is None else min(self._cap, cap)
                break
        else:
            if temp_c < THERMAL_RECOVER_C:
                new = None
        if new != self._cap:
            self.emit("thermal_step", temp_c=round(temp_c, 1), from_cap=self._cap, cap_fps=new)
            self._cap = new
        return self._cap

    def vision_fps_cap(self) -> float | None:
        now = time.monotonic()
        if now - self._polled >= self._poll_s:
            self._polled = now
            try:
                t = self._temp_fn()
            except Exception:  # noqa: BLE001
                t = None
            if t is not None:
                self.observe_temp(float(t))
        return self._cap

    # -- Governor protocol -----------------------------------------------------------------
    def models(self) -> list[dict]:
        return self.registry.items()

    async def switch_genai(self, model: str) -> None:
        key = f"vlm:{model}" if model in VLM_MODELS else f"ollama:{model}"
        if model not in self.registry.ids():
            raise ValueError(f"unknown model {model!r}")
        async with self.slot.acquire(key):
            pass

    def genai_owner(self) -> str | None:
        return self.slot.owner

    def genai_state(self) -> str:
        return self.slot.state

    def npu_stats(self) -> dict:
        return self.pool.stats()

    def model_stats(self) -> list[dict]:
        return self.registry.stats()


def pi_temp_c() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None
