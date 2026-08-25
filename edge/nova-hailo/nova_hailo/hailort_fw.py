"""HailoRT firmware → Model Zoo GenAI HEF URL.

HEFs on ``dev-public.hailo.ai`` are compiled per HailoRT line (5.1.1 HEF will
not load on 5.3.0 firmware, and vice versa). ``scripts/fetch_models.sh``
parses ``hailortcli fw-control identify`` and downloads the matching blob.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping

# Public zoo tags we have confirmed (Instruct HEF exists on each of these).
# 5.1.x → v5.1.1 (the 5.1 line's public tag is 5.1.1, not 5.1.0).
_ZOO_TAG_BY_MINOR: dict[tuple[int, int], str] = {
    (5, 1): "v5.1.1",
    (5, 2): "v5.2.0",
    (5, 3): "v5.3.0",
}

_FW_RE = re.compile(r"Firmware Version:\s*(\d+\.\d+\.\d+)")
_CLI_VER_RE = re.compile(r"(\d+\.\d+\.\d+)")

DEFAULT_LLM_HEF = "Qwen2-1.5B-Instruct.hef"
DEFAULT_FC_HEF = "Qwen2-1.5B-Instruct-Function-Calling-v1.hef"
DEFAULT_FIRMWARE = "5.3.0"
ZOO_BLOB_BASE = "https://dev-public.hailo.ai"
HEF_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.hef$")


def normalize_firmware(version: str) -> str:
    v = (version or "").strip().lstrip("vV")
    m = _CLI_VER_RE.search(v)
    if not m:
        raise ValueError(f"not a HailoRT version: {version!r}")
    return m.group(1)


def zoo_tag_for_firmware(version: str) -> str:
    """Map ``5.3.0`` / ``v5.3.0`` → zoo path tag ``v5.3.0``."""
    major_s, minor_s, *rest = normalize_firmware(version).split(".")
    major, minor = int(major_s), int(minor_s)
    if (major, minor) in _ZOO_TAG_BY_MINOR:
        return _ZOO_TAG_BY_MINOR[(major, minor)]
    patch = int(rest[0]) if rest else 0
    return f"v{major}.{minor}.{patch}"


def llm_hef_url(zoo_tag: str, hef_name: str = DEFAULT_LLM_HEF) -> str:
    tag = normalize_firmware(zoo_tag)
    return f"{ZOO_BLOB_BASE}/v{tag}/blob/{hef_name}"


def parse_fw_control_identify(text: str) -> str | None:
    """Extract ``5.3.0`` from ``hailortcli fw-control identify`` stdout."""
    m = _FW_RE.search(text or "")
    return m.group(1) if m else None


def detect_firmware(
    identify_output: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve firmware: env override → identify text → hailortcli → default.

    ``HAILO_LLM_ZOO_VERSION`` / ``HAILO_RT_VERSION`` skip device probing
    (laptop fetch, or pinning a zoo tag without a Hailo card).
    """
    environ = os.environ if env is None else env
    for key in ("HAILO_LLM_ZOO_VERSION", "HAILO_RT_VERSION"):
        raw = (environ.get(key) or "").strip()
        if raw:
            return normalize_firmware(raw)

    if identify_output:
        parsed = parse_fw_control_identify(identify_output)
        if parsed:
            return parsed

    try:
        proc = subprocess.run(
            ["hailortcli", "fw-control", "identify"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        parsed = parse_fw_control_identify(proc.stdout or "") or parse_fw_control_identify(
            proc.stderr or ""
        )
        if parsed:
            return parsed
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    try:
        proc = subprocess.run(
            ["hailortcli", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in blob.splitlines():
            if re.search(r"(?i)hailort|firmware", line):
                m = _CLI_VER_RE.search(line)
                if m:
                    return m.group(1)
        # Bare ``hailortcli --version`` often prints only ``5.3.0``.
        stripped = blob.strip()
        if _CLI_VER_RE.fullmatch(stripped):
            return stripped
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return DEFAULT_FIRMWARE
