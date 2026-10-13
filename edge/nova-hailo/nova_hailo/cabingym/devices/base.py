# nova_hailo/cabingym/devices/base.py
"""Device/API framework. Handlers are pure: (context, grounded args) → targets + speak.

Callers: every device module, cabingym.grounding, .executor, .router_*.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ApiContext:
    snapshot: Mapping[str, Any]
    nav: Any
    powertrain: str
    hybrid: bool = False
    route_ahead: tuple[tuple[float, float], ...] = ()   # (lat, lon) from the car to the destination


@dataclass
class ApiResult:
    targets: dict[str, Any] = field(default_factory=dict)
    speak: str = ""
    status: str = "success"
    data: Any = None


def done(speak: str, targets: Mapping[str, Any] | None = None, data: Any = None) -> ApiResult:
    return ApiResult(dict(targets or {}), speak, "success", data)


def clarify(speak: str) -> ApiResult:
    return ApiResult({}, speak, "clarify", None)


Handler = Callable[[ApiContext, dict[str, Any]], ApiResult]


@dataclass(frozen=True)
class Api:
    name: str
    description: str
    handler: Handler
    params: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    requires_any: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    read_only: bool = False
    needs_live_nav: bool = False

    def json_schema(self) -> dict[str, Any]:
        props = {
            name: {k: v for k, v in spec.items() if not k.startswith("x-")}
            for name, spec in self.params.items()
        }
        return {"type": "object", "properties": props, "required": list(self.required)}


@dataclass(frozen=True)
class Device:
    name: str
    description: str
    keywords: str
    apis: Mapping[str, Api]


def device(name: str, description: str, keywords: str, apis: Sequence[Api]) -> Device:
    return Device(name, description, keywords, {a.name: a for a in apis})


def p_zone(allowed: Sequence[str], default: str | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "string", "enum": list(allowed), "x-ground": "zone",
                            "description": "Which zone of the cabin."}
    if default is not None:
        spec["default"] = default
    return spec


def p_enum(
    allowed: Sequence[str], *, desc: str, synonyms: Mapping[str, Sequence[str]] | None = None,
    default: str | None = None, bool_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "string", "enum": list(allowed), "x-ground": "enum",
                            "description": desc}
    if synonyms:
        spec["x-synonyms"] = {k: list(v) for k, v in synonyms.items()}
    if default is not None:
        spec["default"] = default
    if bool_map:
        spec["x-bool-map"] = dict(bool_map)
    return spec


def p_bool(desc: str, *, parser: str = "on_off", default: bool | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "boolean", "x-ground": f"bool:{parser}", "description": desc}
    if default is not None:
        spec["default"] = default
    return spec


def p_value(parser: str, desc: str, lo: float | None = None, hi: float | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "number", "x-ground": parser, "description": desc}
    if lo is not None:
        spec["minimum"] = lo
    if hi is not None:
        spec["maximum"] = hi
    return spec


def p_delta(parser: str, desc: str) -> dict[str, Any]:
    return {"type": "number", "x-ground": parser, "description": desc}


def p_entity(kind: str, desc: str) -> dict[str, Any]:
    return {"type": "string", "x-ground": f"entity:{kind}", "description": desc}


def p_span(kind: str, desc: str) -> dict[str, Any]:
    return {"type": "string", "x-ground": f"span:{kind}", "description": desc}


def p_place(desc: str) -> dict[str, Any]:
    return {"type": "string", "x-ground": "place", "description": desc}


ZONE_LABEL = {
    "driver": "Driver side", "passenger": "Passenger side", "all": "Both sides",
    "front": "Front", "rear": "Rear", "rear_left": "Rear left", "rear_right": "Rear right",
}


def fmt_num(value: float) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def resolve_value(args: Mapping[str, Any], current: Any) -> float:
    if args.get("value") is not None:
        return float(args["value"])
    return float(current) + float(args.get("delta") or 0.0)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def fmt_minutes(minutes: float) -> str:
    m = max(1, int(round(minutes)))
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, rem = divmod(m, 60)
    hours = f"{h} hour{'s' if h != 1 else ''}"
    return hours if rem == 0 else f"{hours} {rem} minute{'s' if rem != 1 else ''}"
