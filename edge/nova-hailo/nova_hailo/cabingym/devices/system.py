# nova_hailo/cabingym/devices/system.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clamp, device, done, p_delta, p_enum, p_value, resolve_value,
)

CAPABILITIES = ("I can control the climate, seats, windows, doors, lights, sunroof and wipers, "
                "music, navigation and your phone, and tell you the car's status.")


def _connectivity(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    net = ctx.snapshot[c.CONNECTIVITY]
    if net == "none":
        return done("I'm offline right now, but car controls still work.")
    return done(f"You're on {net.upper()}.")


def _brightness(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    level = int(clamp(resolve_value(args, ctx.snapshot[c.BRIGHTNESS]), 0, 100))
    return done(f"Screen brightness {level} percent.", {c.BRIGHTNESS: level})


DEVICE = device(
    "system", "system settings: connectivity, screen brightness and drive mode",
    r"\b(screen|display|bright\w*|dim|mode|internet|network|online|offline)\b",
    [
        Api("get_drive_mode", "tell the current drive mode", lambda ctx, args: done(
                f"You're in {ctx.snapshot[c.DRIVE_MODE]} mode."), read_only=True,
            patterns=(r"\b(what|which)\b.*\b(drive|driving)\s+mode\b",)),
        Api("set_drive_mode", "switch drive mode to eco, comfort or sport", lambda ctx, args: done(
                f"{args['mode'].capitalize()} mode on.", {c.DRIVE_MODE: args["mode"]}),
            params={"mode": p_enum(("eco", "comfort", "sport"), desc="Drive mode")},
            required=("mode",), writes=(c.DRIVE_MODE,),
            patterns=(r"\b(eco|comfort|sport)\s+mode\b|\b(drive|driving)\s+mode\b",)),
        Api("set_display_brightness", "change the screen brightness", _brightness,
            params={"value": p_value("percent", "Brightness percent", 0, 100),
                    "delta": p_delta("percent", "Change in brightness")},
            requires_any=("value", "delta"), writes=(c.BRIGHTNESS,),
            patterns=(r"\b(screen|display)\b.*\b(bright\w*|dim\w*)\b|\b(bright\w*|dim)\b.*\b(screen|display)\b",)),
        Api("get_connectivity", "tell whether the car is online", _connectivity, read_only=True,
            patterns=(r"\b(internet|connectivity|network|signal\s+strength|am\s+i\s+online|"
                      r"are\s+we\s+online|offline)\b",)),
        Api("report_capabilities", "list what the car assistant can control", lambda ctx, args: done(
                CAPABILITIES), read_only=True,
            patterns=(r"\bwhat\s+can\s+(you|i)\s+(do|control)\s+(in|with)\s+(the|this)\s+car\b|"
                      r"\bcar\s+(features|functions|controls)\b",)),
    ],
)
