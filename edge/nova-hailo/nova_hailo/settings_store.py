"""Persisted HMI LLM settings (runtime/, not config.yaml)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nova_hailo.config import ROOT

SETTINGS_PATH = ROOT / "runtime" / "hmi_settings.json"

OR_MODELS = (
    {
        "id": "deepseek/deepseek-v4-flash-0731",
        "label": "DeepSeek V4 Flash",
        "hint": "default · fastest full tool turn · tools",
    },
    {
        "id": "meta-llama/llama-3.3-70b-instruct",
        "label": "Llama 3.3 70B",
        "hint": "fastest first token · does NOT tool-call here",
    },
    {
        "id": "deepseek/deepseek-v3.2",
        "label": "DeepSeek V3.2",
        "hint": "stronger chat · still cheap",
    },
    {
        "id": "qwen/qwen3.8-flash",
        "label": "Qwen3.8 Flash",
        "hint": "tools · ~$0.15/0.47 · slower TTFT",
    },
    {
        "id": "z-ai/glm-4.7-flash",
        "label": "GLM 4.7 Flash",
        "hint": "tools · cheap · fast",
    },
    {
        "id": "thinkingmachines/inkling-small",
        "label": "Inkling Small",
        "hint": "tools · mid cost · low TTFT",
    },
)

LOCAL_MODELS = (
    {"id": "qwen2", "label": "Qwen2 1.5B", "hint": "default Hailo HEF"},
    {"id": "qwen25", "label": "Qwen2.5 1.5B", "hint": "Hailo zoo"},
    {"id": "qwen3", "label": "Qwen3 1.7B", "hint": "Hailo zoo"},
    {"id": "llama32", "label": "Llama 3.2 1B", "hint": "Hailo zoo"},
    {"id": "qwen2-fc", "label": "Qwen2 FC", "hint": "function-calling HEF"},
    {"id": "deepseek", "label": "DeepSeek R1 distill 1.5B", "hint": "Hailo zoo"},
)

DEFAULT_OR = OR_MODELS[0]["id"]
DEFAULT_LOCAL = "qwen2"


def catalog() -> dict[str, Any]:
    return {
        "local_models": [dict(m) for m in LOCAL_MODELS],
        "or_models": [dict(m) for m in OR_MODELS],
        "default_local": DEFAULT_LOCAL,
        "default_or": DEFAULT_OR,
    }


def _normalize_connectors(raw: Any) -> list[dict[str, Any]]:
    """Coerce whatever is on disk into a safe connector list.

    A hand-edited or older settings file may have no "connectors" key at
    all, or a malformed one -- either must load as an empty list rather
    than raise, so existing settings files keep loading unchanged.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        url = str(item.get("url") or "").strip()
        if not cid or not url:
            continue
        tools = item.get("tools")
        out.append(
            {
                "id": cid,
                "kind": str(item.get("kind") or "mcp_http"),
                "label": str(item.get("label") or ""),
                "url": url,
                "enabled": bool(item.get("enabled", False)),
                "tools": [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else [],
            }
        )
    return out


def load_settings(path: Path | None = None) -> dict[str, Any]:
    p = path or SETTINGS_PATH
    data: dict[str, Any] = {
        "mode": "local",
        "local_hef": DEFAULT_LOCAL,
        "or_model": DEFAULT_OR,
        "connectors": [],
    }
    try:
        if p.is_file():
            raw = json.loads(p.read_text())
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in data if k in raw})
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    mode = str(data.get("mode") or "local").strip().lower()
    data["mode"] = "openrouter" if mode in {"openrouter", "or", "cloud"} else "local"
    data["local_hef"] = str(data.get("local_hef") or DEFAULT_LOCAL)
    data["or_model"] = str(data.get("or_model") or DEFAULT_OR)
    data["connectors"] = _normalize_connectors(data.get("connectors"))
    return data


def save_settings(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or SETTINGS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = load_settings(p)
    cur.update(
        {k: data[k] for k in ("mode", "local_hef", "or_model", "connectors") if k in data}
    )
    mode = str(cur.get("mode") or "local").strip().lower()
    cur["mode"] = "openrouter" if mode in {"openrouter", "or", "cloud"} else "local"
    cur["connectors"] = _normalize_connectors(cur.get("connectors"))
    p.write_text(json.dumps(cur, indent=2) + "\n")
    return p
