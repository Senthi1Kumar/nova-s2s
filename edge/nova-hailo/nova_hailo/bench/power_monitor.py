"""Best-effort Hailo/CPU power sampling (adapted from hailo_ollama_bench)."""
from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable


class PowerMonitor:
    def __init__(self, callback: Callable[[float], None] | None = None):
        self._callback = callback
        self._running = False
        self._thread: threading.Thread | None = None
        self._hailortcli_available = False
        self._power_samples: list[float] = []
        self._hailo_tdp_w = 10.0
        self._lock = threading.Lock()
        self._check_hailortcli()

    def _check_hailortcli(self):
        try:
            result = subprocess.run(
                ["hailortcli", "measure-power", "--help"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            self._hailortcli_available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            self._hailortcli_available = False

    def _read_cpu_temp(self) -> float | None:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _estimate_power_from_temp(self, temp_c: float, idle_temp: float = 50.0) -> float:
        if temp_c <= idle_temp:
            return 2.0
        load_ratio = min((temp_c - idle_temp) / 30.0, 1.0)
        idle_w = 2.0
        peak_w = self._hailo_tdp_w
        return idle_w + (peak_w - idle_w) * load_ratio

    def _measure_power_via_hailortcli(self, duration_s: float = 2.0) -> float | None:
        if not self._hailortcli_available:
            return None
        try:
            result = subprocess.run(
                [
                    "hailortcli",
                    "measure-power",
                    "--duration",
                    str(int(duration_s)),
                    "--sampling-period",
                    "1100",
                    "--averaging-factor",
                    "256",
                ],
                capture_output=True,
                text=True,
                timeout=duration_s + 3,
            )
            for line in result.stdout.splitlines():
                line = line.strip().lower()
                if "power" in line and "w" in line:
                    for p in line.split():
                        try:
                            val = float(p)
                        except ValueError:
                            continue
                        if 0 < val < 100:
                            return val
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
            return None

    def _collect_loop(self):
        while self._running:
            power = self._measure_power_via_hailortcli(2.0)
            if power is None:
                temp = self._read_cpu_temp()
                if temp is not None:
                    power = self._estimate_power_from_temp(temp)
            if power is not None:
                with self._lock:
                    self._power_samples.append(power)
                    if len(self._power_samples) > 150:
                        self._power_samples = self._power_samples[-150:]
                if self._callback:
                    try:
                        self._callback(power)
                    except Exception:
                        pass
            time.sleep(2.0)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True, name="power-monitor")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def latest_w(self) -> float | None:
        with self._lock:
            return self._power_samples[-1] if self._power_samples else None

    @property
    def avg_power_w(self) -> float | None:
        with self._lock:
            if not self._power_samples:
                return None
            return sum(self._power_samples) / len(self._power_samples)

    @property
    def peak_power_w(self) -> float | None:
        with self._lock:
            return max(self._power_samples) if self._power_samples else None

    @property
    def idle_power_w(self) -> float | None:
        with self._lock:
            return min(self._power_samples) if self._power_samples else None

    @property
    def power_samples(self) -> list[float]:
        with self._lock:
            return list(self._power_samples)
