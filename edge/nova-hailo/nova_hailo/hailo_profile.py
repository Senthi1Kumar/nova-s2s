"""Optional HailoRT monitor hooks.

IMPORTANT (Hailo-10H + HailoRT 5.1.1):
  Community + device investigation:

  ~/docs and https://community.hailo.ai/t/hailo-10h-hailort-5-1-1-hailo-monitor-not-working/18518

  - HAILO_MONITOR=1 / hailortcli monitor are for Hailo-8/8L style network-group
    monitoring. On Hailo-10H GenAI they often do **nothing useful** or are
    unsupported.
  - Power/temp ioctls may return HAILO_INVALID_OPERATION on 10H AI Hat.
  - DRAM utilization is not exposed on 5.1.1.

For GenAI (Whisper/LLM stream) use **application-side metrics** in nova_hailo.metrics
(TTFT, prefill/decode, tok/s). That is the correct profiler for this stack.

HailoRT 5.3.0 may improve tooling; upgrading FW/HEFs together is required.
"""
from __future__ import annotations

import os
import subprocess

MONITOR_SUPPORTED_NOTE = (
    "HAILO_MONITOR is Hailo-8/8L oriented; often unsupported on Hailo-10H 5.1.1. "
    "Use nova-hailo turn metrics for ASR/LLM/TTS profiling."
)


def enable_hailo_monitor_env() -> str:
    """Set HAILO_MONITOR=1. May have no effect on 10H — documented for completeness."""
    os.environ["HAILO_MONITOR"] = "1"
    return MONITOR_SUPPORTED_NOTE


def print_monitor_instructions():
    print(
        """
[hailo-profile]
  App-side metrics: always on (TTFT / decode / STT split / TTS synth+play).

  Hailo monitor (often Hailo-8 only):
    export HAILO_MONITOR=1
    # then in another terminal while app runs:
    hailortcli monitor

  On Hailo-10H + HailoRT 5.1.1 this is usually non-functional
  (community: monitor/not-working thread). Prefer app metrics.

  Power (may fail on AI Hat):
    hailortcli measure-power --duration 5

  FW check:
    hailortcli fw-control identify
"""
    )


def try_fw_identify() -> str | None:
    try:
        out = subprocess.check_output(
            ["hailortcli", "fw-control", "identify"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        return out.strip()
    except Exception as e:
        return f"(identify failed: {e})"
