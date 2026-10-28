"""Host + NPU + server telemetry snapshots (spec section 7)."""

from __future__ import annotations

import asyncio
import logging
import threading
import shutil
import subprocess
import time
from collections import deque

from .services import Services

_VCGEN_TTL_S = 1.0
log = logging.getLogger("aibox.telemetry")


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _cpu_times() -> list[tuple[int, int]]:
    """[(idle, total)] for aggregate then each core."""
    out: list[tuple[int, int]] = []
    txt = _read("/proc/stat") or ""
    for line in txt.splitlines():
        if not line.startswith("cpu"):
            continue
        vals = [int(x) for x in line.split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        out.append((idle, sum(vals[:8])))
    return out


def _vcgencmd(*args: str) -> str | None:
    exe = shutil.which("vcgencmd")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=1.0)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class Telemetry:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.started = time.time()
        self.requests_total = 0
        self._req_times: deque[float] = deque(maxlen=4096)
        self._prev_cpu = _cpu_times()
        self._vc: dict = {}
        self._vc_at = 0.0
        self.latest: dict | None = None
        self._lock = threading.RLock()  # guards counters + cpu/vcgencmd state

    def note_request(self) -> None:
        with self._lock:
            self.requests_total += 1
            self._req_times.append(time.monotonic())

    def _rps(self, window: float = 10.0) -> float:
        now = time.monotonic()
        with self._lock:
            times = list(self._req_times)
        n = sum(1 for t in times if now - t <= window)
        return round(n / window, 3)

    def _cpu(self) -> tuple[float, list[float]]:
        cur = _cpu_times()
        prev, self._prev_cpu = self._prev_cpu, cur
        pcts: list[float] = []
        for (i0, t0), (i1, t1) in zip(prev, cur, strict=False):
            dt = t1 - t0
            pcts.append(round(100.0 * (1 - (i1 - i0) / dt), 1) if dt > 0 else 0.0)
        if not pcts:
            return 0.0, []
        return pcts[0], pcts[1:]

    def _vcgen(self) -> dict:
        now = time.monotonic()
        if now - self._vc_at < _VCGEN_TTL_S and self._vc:
            return self._vc
        temp = arm = thr = None
        t = _vcgencmd("measure_temp")
        if t and "=" in t:
            try:
                temp = float(t.split("=")[1].split("'")[0])
            except ValueError:
                pass
        c = _vcgencmd("measure_clock", "arm")
        if c and "=" in c:
            try:
                arm = round(int(c.split("=")[1]) / 1e6, 1)
            except ValueError:
                pass
        g = _vcgencmd("get_throttled")
        if g and "=" in g:
            thr = g.split("=")[1].strip()
        self._vc = {"temp_c": temp, "arm_mhz": arm, "throttled": thr}
        self._vc_at = now
        return self._vc

    def _mem(self) -> tuple[float, float]:
        info: dict[str, int] = {}
        for line in (_read("/proc/meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            try:
                info[k] = int(v.split()[0])
            except (ValueError, IndexError):
                pass
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return round((total - avail) / 1024.0, 1), round(total / 1024.0, 1)

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        gov = self.services.governor
        cpu_pct, per_core = self._cpu()
        used, total = self._mem()
        try:
            load_1m = float((_read("/proc/loadavg") or "0").split()[0])
        except (ValueError, IndexError):
            load_1m = None
        vc = self._vcgen()
        npu = dict(gov.npu_stats())
        npu["genai_owner"] = gov.genai_owner()
        npu["genai_state"] = gov.genai_state()
        return {
            "t": time.time(),
            "host": {
                "cpu_pct": cpu_pct,
                "cpu_per_core": per_core,
                "mem_used_mb": used,
                "mem_total_mb": total,
                "temp_c": vc["temp_c"],
                "arm_mhz": vc["arm_mhz"],
                "throttled": vc["throttled"],
                "load_1m": load_1m,
            },
            "npu": npu,
            "models": gov.model_stats(),
            "server": {
                "rps": self._rps(),
                "clients": len(self.services.inspector.clients()),
                "requests_total": self.requests_total,
                "uptime_s": round(time.time() - self.started, 1),
            },
        }

    async def current(self, max_age: float = 2.5) -> dict:
        """Latest sample; stale/missing samples are refreshed off the event loop."""
        if self.latest is None or time.time() - self.latest["t"] > max_age:
            self.latest = await asyncio.to_thread(self.snapshot)
        return self.latest

    async def run(self, interval: float = 1.0) -> None:
        while True:
            try:
                self.latest = await asyncio.to_thread(self.snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the sampler must survive any one bad sample
                log.exception("telemetry sample failed; continuing")
            await asyncio.sleep(interval)
