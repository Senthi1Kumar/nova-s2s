"""OpenAI-style tool schemas from the OEM allowlist (OpenRouter / cloud LLM).

Host still executes via ToolBroker. MCP URLs and keys never leave the Pi.
Identity / smalltalk / capability are not exposed as tools.
"""
from __future__ import annotations

from typing import Any

from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.types import Intent

# Intents the cloud model may request. Canned chat stays host-side or free text.
_CLOUD_TOOLS: dict[str, dict[str, Any]] = {
    "web_search": {
        "description": (
            "Search the live web for current facts, news, places, prices, or people. "
            "Use when the answer is not already in the conversation."
        ),
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query, short and specific.",
            }
        },
        "required": ["query"],
    },
    "deep_research": {
        "description": (
            "Longer web research with citations. Use only when the user asks to "
            "research, go deeper, or wants a report — not for a simple lookup."
        ),
        "properties": {
            "query": {
                "type": "string",
                "description": "Research question.",
            }
        },
        "required": ["query"],
    },
    "check_calendar": {
        "description": "Read the user's calendar (today, tomorrow, or this week).",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up, e.g. meetings today.",
            },
            "day": {
                "type": "string",
                "description": "Optional window: today, tomorrow, or week.",
            },
        },
        "required": ["query"],
    },
    "check_email": {
        "description": "Search recent email (read-only).",
        "properties": {
            "query": {
                "type": "string",
                "description": "Sender, subject, or keywords.",
            }
        },
        "required": ["query"],
    },
    "list_drive_files": {
        "description": "List or search Google Drive files (read-only).",
        "properties": {
            "query": {
                "type": "string",
                "description": "File name or topic. Empty lists recent files.",
            }
        },
        "required": [],
    },
    "research_status": {
        "description": "Poll an in-progress deep_research job.",
        "properties": {
            "job_id": {"type": "string", "description": "Job id from deep_research."}
        },
        "required": ["job_id"],
    },
}


def openai_tools_for_profile(profile: CapabilityProfile | None) -> list[dict[str, Any]]:
    """Allowlisted function tools in OpenAI chat.completions shape."""
    enabled = profile.enabled if profile is not None else frozenset()
    out: list[dict[str, Any]] = []
    for name, spec in _CLOUD_TOOLS.items():
        try:
            intent = Intent(name)
        except ValueError:
            continue
        if profile is not None and not profile.allows(intent):
            continue
        if name not in enabled:
            continue
        props = spec["properties"]
        required = list(spec.get("required") or [])
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec["description"],
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            }
        )
    return out


def tool_call_kwargs(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Map model arguments onto OemToolGateway.execute(**kwargs)."""
    args = dict(arguments or {})
    kwargs: dict[str, Any] = {"query": str(args.get("query") or "")}
    if name == "research_status":
        kwargs["job_id"] = str(args.get("job_id") or "")
    elif name == "check_calendar" and args.get("day"):
        kwargs["day"] = str(args["day"])
    return kwargs
