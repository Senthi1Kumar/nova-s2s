# nova_hailo/cabingym/cloud_tools.py
"""Device-level function tools for the cloud LLM path.

One tool per device (``vehicle_hvac`` with an ``action`` enum) instead of
57 flat schemas keeps the prompt small. The host still grounds, gates and
executes every call. Callers: edge_harness.openai_tools, OemToolGateway.execute.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nova_hailo.cabingym.devices.base import Device
from nova_hailo.cabingym.grounding import clarify_speak, decline_speak, normalize_cloud_args
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall

PREFIX = "vehicle_"


def _merge(old: Mapping[str, Any] | None, new: Mapping[str, Any], action: str,
           first_action: str | None) -> dict[str, Any]:
    """One parameter shared by several actions of a device.

    The same name can mean different things per action (``value`` is degrees for
    set_temperature but a fan level for set_fan; 100 is open for the sunroof but
    closed for the sunshade), so differing descriptions are kept per action.
    """
    if old is None:
        return dict(new)
    out: dict[str, Any] = {"type": old.get("type", new.get("type"))}
    old_desc = str(old.get("description", ""))
    new_desc = str(new.get("description", ""))
    if new_desc and new_desc not in old_desc:
        if first_action and not old_desc.startswith(f"{first_action}:"):
            old_desc = f"{first_action}: {old_desc}" if old_desc else ""
        old_desc = f"{old_desc}; {action}: {new_desc}" if old_desc else f"{action}: {new_desc}"
    out["description"] = old_desc
    if "enum" in old and "enum" in new:
        out["enum"] = list(dict.fromkeys([*old["enum"], *new["enum"]]))
    return out


def vehicle_tool_schemas(devices: Mapping[str, Device]) -> list[dict[str, Any]]:
    tools = []
    for name, dev in devices.items():
        props: dict[str, Any] = {"action": {
            "type": "string", "enum": list(dev.apis),
            "description": "; ".join(f"{a.name}: {a.description}" for a in dev.apis.values()),
        }}
        first_owner: dict[str, str] = {}
        for api in dev.apis.values():
            for pname, spec in api.json_schema()["properties"].items():
                props[pname] = _merge(props.get(pname), spec, api.name, first_owner.get(pname))
                first_owner.setdefault(pname, api.name)
        tools.append({"type": "function", "function": {
            "name": f"{PREFIX}{name}",
            "description": (f"Control or read the car's {dev.description}. Pick one action; "
                            "the car checks its safety rules before acting."),
            "parameters": {"type": "object", "properties": props, "required": ["action"]},
        }})
    return tools


def cloud_call(name: str, args: Mapping[str, Any] | None, cabin: CabinEnv) -> RouteOutcome:
    trace: dict[str, Any] = {"router": "cloud", "tool": name}
    dev = cabin.devices.get(name.removeprefix(PREFIX))
    if dev is None:
        return RouteOutcome("decline", speak="That isn't available in this car.", trace=trace)
    raw = dict(args or {})
    action = str(raw.pop("action", "") or "")
    trace["action"] = action
    api = dev.apis.get(action)
    if api is None:
        return RouteOutcome("clarify", speak=f"What should I do with the {dev.name}?", trace=trace)
    if api.needs_live_nav and not cabin.allow_poi_search:
        return RouteOutcome("decline", speak="I can't search for places yet.", trace=trace)
    g = normalize_cloud_args(api, raw, cabin)
    if g.unsupported:
        return RouteOutcome("decline", speak=decline_speak(api, g.unsupported), trace=trace)
    if g.missing:
        trace["partial"] = {"device": dev.name, "api": api.name, "args": dict(g.args)}
        return RouteOutcome(
            "clarify", speak=clarify_speak(api, g.missing, g.unresolved), trace=trace)
    return RouteOutcome("vehicle", [VehicleCall(dev.name, api.name, g.args, g.grounding)], trace=trace)
