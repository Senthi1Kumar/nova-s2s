"""Firmware → Hailo Model Zoo HEF URL (no Hailo hardware)."""
from __future__ import annotations

from nova_hailo.hailort_fw import (
    detect_firmware,
    llm_hef_url,
    parse_fw_control_identify,
    zoo_tag_for_firmware,
)

IDENTIFY_5_3_0 = """
Executing on device: pci/0001:01:00.0
Identifying board
Control Protocol Version: 2
Firmware Version: 5.3.0 (release,app)
Logger Version: 0
Device Architecture: HAILO10H
"""

IDENTIFY_5_1_1 = """
Firmware Version: 5.1.1 (release,app)
Device Architecture: HAILO10H
"""


def test_parse_identify_5_3_0():
    assert parse_fw_control_identify(IDENTIFY_5_3_0) == "5.3.0"


def test_parse_identify_5_1_1():
    assert parse_fw_control_identify(IDENTIFY_5_1_1) == "5.1.1"


def test_zoo_tag_maps_known_lines():
    assert zoo_tag_for_firmware("5.3.0") == "v5.3.0"
    assert zoo_tag_for_firmware("v5.3.1") == "v5.3.0"
    assert zoo_tag_for_firmware("5.2.0") == "v5.2.0"
    assert zoo_tag_for_firmware("5.1.1") == "v5.1.1"
    assert zoo_tag_for_firmware("5.1.0") == "v5.1.1"


def test_llm_hef_url_5_3():
    url = llm_hef_url("v5.3.0")
    assert url == "https://dev-public.hailo.ai/v5.3.0/blob/Qwen2-1.5B-Instruct.hef"


def test_detect_prefers_env_over_identify():
    fw = detect_firmware(IDENTIFY_5_1_1, env={"HAILO_LLM_ZOO_VERSION": "5.3.0"})
    assert fw == "5.3.0"


def test_detect_from_identify_when_no_env():
    fw = detect_firmware(IDENTIFY_5_3_0, env={})
    assert fw == "5.3.0"
    assert zoo_tag_for_firmware(fw) == "v5.3.0"


def test_parse_identify_miss():
    assert parse_fw_control_identify("no firmware here") is None


def test_llm_hef_url_accepts_untagged_version():
    assert llm_hef_url("5.1.1") == (
        "https://dev-public.hailo.ai/v5.1.1/blob/Qwen2-1.5B-Instruct.hef"
    )
    assert llm_hef_url("V5.3.0") == (
        "https://dev-public.hailo.ai/v5.3.0/blob/Qwen2-1.5B-Instruct.hef"
    )


def test_detect_hailo_rt_version_env():
    fw = detect_firmware("", env={"HAILO_RT_VERSION": "v5.1.1"})
    assert fw == "5.1.1"
    assert zoo_tag_for_firmware(fw) == "v5.1.1"
