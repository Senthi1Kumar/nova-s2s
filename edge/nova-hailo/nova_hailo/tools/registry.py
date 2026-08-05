from __future__ import annotations

import json
import platform
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., dict]
    speak_template: str | None = None


def _ok(result: Any = None, speak: str | None = None, **extra) -> dict:
    out = {"ok": True, "result": result}
    if speak:
        out["speak"] = speak
    out.update(extra)
    return out


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg, "speak": msg}


def tool_get_time() -> dict:
    now = datetime.now().strftime("%I:%M %p on %A, %B %d")
    speak = f"It's {now}."
    return _ok(result=now, speak=speak)


def tool_calculator(expression: str = "") -> dict:
    expr = (expression or "").strip()
    if not expr:
        return _err("No expression provided.")
    allowed = set("0123456789+-*/().% eE ")
    if any(c not in allowed for c in expr):
        return _err("I can only evaluate simple arithmetic.")
    try:
        value = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — intentionally restricted
        speak = f"The result is {value}."
        return _ok(result=value, speak=speak)
    except Exception as e:
        return _err(f"Could not calculate that: {e}")


def tool_system_info() -> dict:
    info = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "node": platform.node(),
    }
    speak = f"Running on {info['machine']} {info['system']}, Python {info['python']}."
    return _ok(result=info, speak=speak)


def tool_send_payment(amount: float = 0, currency: str = "USD", recipient: str = "unknown") -> dict:
    if amount <= 0:
        return _err("Payment amount must be positive.")
    result = {
        "status": "submitted",
        "amount": amount,
        "currency": currency,
        "recipient": recipient,
        "mock": True,
    }
    speak = f"Sent {amount} {currency} to {recipient}. This is a mock payment."
    return _ok(result=result, speak=speak)


def build_default_tools(enabled: list[str] | None = None) -> dict[str, ToolSpec]:
    catalog = {
        "get_time": ToolSpec(
            name="get_time",
            description="Get the current local time and date.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda **_: tool_get_time(),
        ),
        "calculator": ToolSpec(
            name="calculator",
            description="Evaluate a simple arithmetic expression.",
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic expression, e.g. '(12+3)*2'",
                    }
                },
                "required": ["expression"],
            },
            handler=lambda expression="", **_: tool_calculator(expression),
        ),
        "system_info": ToolSpec(
            name="system_info",
            description="Report basic host system information.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda **_: tool_system_info(),
        ),
        "send_payment": ToolSpec(
            name="send_payment",
            description="Send a mock payment. Only after auth ACCEPT.",
            parameters={
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Amount to send"},
                    "currency": {"type": "string", "description": "Currency code", "default": "USD"},
                    "recipient": {"type": "string", "description": "Payee name"},
                },
                "required": ["amount", "recipient"],
            },
            handler=lambda amount=0, currency="USD", recipient="unknown", **_: tool_send_payment(
                float(amount), str(currency), str(recipient)
            ),
        ),
    }
    if enabled is None:
        return catalog
    return {k: v for k, v in catalog.items() if k in enabled}


def tools_prompt_block(tools: dict[str, ToolSpec]) -> str:
    lines = [
        "You may call tools using exactly this format:",
        "<tool_call>",
        '{"name": "TOOL_NAME", "arguments": {...}}',
        "</tool_call>",
        "Available tools:",
    ]
    for spec in tools.values():
        lines.append(f"- {spec.name}: {spec.description}")
        lines.append(f"  parameters: {json.dumps(spec.parameters)}")
    lines.append("If no tool is needed, answer the user directly in plain text.")
    return "\n".join(lines)


def execute_tool(name: str, arguments: dict, tools: dict[str, ToolSpec]) -> dict:
    spec = tools.get(name)
    if not spec:
        return _err(f"Unknown tool: {name}")
    try:
        args = arguments or {}
        return spec.handler(**args)
    except TypeError as e:
        return _err(f"Bad arguments for {name}: {e}")
    except Exception as e:
        return _err(f"Tool {name} failed: {e}")
