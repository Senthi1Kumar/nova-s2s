# nova_hailo/cabingym/devices/lights.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clamp, device, done, p_bool, p_delta, p_enum, p_value, p_zone,
    resolve_value,
)

HEADLIGHT_SPEAK = {"off": "Headlights off.", "auto": "Headlights on auto.",
                   "low": "Low beams on.", "high": "High beams on."}
COLORS = ("white", "blue", "red", "green", "purple", "orange", "pink")


def _headlights(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    mode = args["mode"]
    return done(HEADLIGHT_SPEAK[mode], {c.HEADLIGHTS: mode})


def _interior(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    zone = args.get("zone", "front")
    on = bool(args.get("on", True))
    paths = {"front": (c.DOME,), "rear": (c.REAR_READING,), "all": (c.DOME, c.REAR_READING)}[zone]
    label = {"front": "Front light", "rear": "Rear reading lights", "all": "Interior lights"}[zone]
    return done(f"{label} {'on' if on else 'off'}.", dict.fromkeys(paths, on))


def _ambient(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    targets: dict[str, Any] = {}
    parts = []
    if args.get("color"):
        targets[c.AMBIENT_COLOR] = args["color"]
        parts.append(f"set to {args['color']}")
    if args.get("value") is not None or args.get("delta") is not None:
        level = int(clamp(resolve_value(args, ctx.snapshot[c.AMBIENT_LEVEL]), 0, 100))
        targets[c.AMBIENT_LEVEL] = level
        parts.append(f"at {level} percent")
    return done(f"Ambient light {' and '.join(parts)}.", targets)


DEVICE = device(
    "lights", "headlights, hazard lights, interior and ambient lights",
    r"\b(lights?|lighting|head\s*lights?|beams?|hazards?|ambient|dome|brights|flashers)\b",
    [
        Api("set_hazards", "turn the hazard lights on or off",
            lambda ctx, args: done("Hazards on." if args.get("on", True) else "Hazards off.",
                                   {c.HAZARDS: bool(args.get("on", True))}),
            params={"on": p_bool("Hazards on", default=True)}, writes=(c.HAZARDS,),
            patterns=(r"\b(hazards?|flashers|warning\s+lights)\b",)),
        Api("set_ambient_light", "change the ambient light colour or brightness", _ambient,
            params={"color": p_enum(COLORS, desc="Ambient colour",
                                    synonyms={"purple": ("violet",), "orange": ("amber",)}),
                    "value": p_value("percent", "Brightness percent", 0, 100),
                    "delta": p_delta("percent", "Change in brightness")},
            requires_any=("color", "value", "delta"),
            writes=(c.AMBIENT_COLOR, c.AMBIENT_LEVEL),
            patterns=(r"\bambient\b|\bmood\s+light",)),
        Api("set_interior_light", "turn the interior or reading lights on or off", _interior,
            params={"zone": p_zone(("front", "rear", "all"), "front"),
                    "on": p_bool("Lights on", default=True)},
            writes=(c.DOME, c.REAR_READING),
            patterns=(r"\b(interior|cabin|dome|reading|inside|map)\s+lights?\b|"
                      r"\blights?\s+(inside|in\s+here|in\s+the\s+back)\b",)),
        Api("set_headlights", "change the headlights: off, auto, low or high beams", _headlights,
            params={"mode": p_enum(("off", "auto", "low", "high"), desc="Headlight mode",
                                   synonyms={"auto": ("automatic",),
                                             "low": ("low beams", "low beam", "dipped"),
                                             "high": ("high beams", "high beam", "brights",
                                                      "full beam", "main beam")},
                                   bool_map={"true": "low", "false": "off"})},
            required=("mode",), writes=(c.HEADLIGHTS,),
            patterns=(r"\b(head\s*lights?|high\s+beams?|low\s+beams?|brights|full\s+beam|dipped)\b",)),
    ],
)
