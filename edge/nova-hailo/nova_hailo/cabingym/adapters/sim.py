# nova_hailo/cabingym/adapters/sim.py
from __future__ import annotations

import math
from typing import Any

from nova_hailo.cabingym.vss import Change, VssStore

# Units per second when ramping (HMI demo only; scoring always settles first).
RAMP_RATES: dict[str, float] = {"celsius": 0.5, "percent": 20.0}


class InProcessSim:
    """Deterministic simulator: actuator targets become current values.

    ``ramp=True`` moves temperature/percent actuators toward their target in
    tick(); everything else is still immediate.
    """

    name = "sim"

    def __init__(self, *, ramp: bool = False):
        self.ramp = ramp
        self._store: VssStore | None = None
        self._pending: dict[str, Any] = {}

    def attach(self, store: VssStore) -> None:
        self._store = store
        store.target_sink = self._on_target

    def _on_target(self, path: str, value: Any) -> Any:
        if not self.ramp or self._store is None:
            return value
        sig = self._store.signal(path)
        if sig.dtype in ("float", "int") and sig.unit in RAMP_RATES:
            self._pending[path] = value
            return None
        return value

    def tick(self, dt_s: float) -> list[Change]:
        if self._store is None or not self._pending:
            return []
        updates: dict[str, Any] = {}
        for path, target in list(self._pending.items()):
            sig = self._store.signal(path)
            cur = self._store.get(path)
            step = RAMP_RATES[sig.unit or ""] * dt_s
            if sig.dtype == "int":
                step = max(step, 1.0)
            nxt = target if abs(target - cur) <= step else cur + math.copysign(step, target - cur)
            if sig.dtype == "int":
                nxt = int(round(nxt))
            updates[path] = nxt
            if nxt == target:
                del self._pending[path]
        return self._store.update(updates)

    def settle(self) -> list[Change]:
        if self._store is None or not self._pending:
            return []
        updates = dict(self._pending)
        self._pending.clear()
        return self._store.update(updates)
