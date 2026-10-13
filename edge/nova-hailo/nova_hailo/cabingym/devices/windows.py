# nova_hailo/cabingym/devices/windows.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clamp, device, done, p_value, p_zone,
)

ZONES = ("driver", "passenger", "rear_left", "rear_right", "front", "rear", "all")
WINDOW_LABEL = {
    "driver": "Driver window", "passenger": "Passenger window", "rear_left": "Rear left window",
    "rear_right": "Rear right window", "front": "Front windows", "rear": "Rear windows",
    "all": "Windows",
}


def _set_window(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    zone = args.get("zone", "driver")
    value = int(clamp(float(args["value"]), 0, 100))
    targets = {c.window(side): value for side in c.WINDOW_SIDES[zone]}
    what = WINDOW_LABEL[zone]
    if value == 0:
        return done(f"{what} closed.", targets)
    if value == 100:
        return done(f"{what} open.", targets)
    return done(f"{what} opened to {value} percent.", targets)


def _close_all(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    return done("All windows closed.", {c.window(side): 0 for side in c.DOOR_SIDES})


def _rear_shade(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    value = int(clamp(float(args["value"]), 0, 100))
    speak = {100: "Rear shade up.", 0: "Rear shade down."}.get(value, f"Rear shade at {value} percent.")
    return done(speak, {c.REAR_SHADE: value})


DEVICE = device(
    "windows", "windows and the rear sunshade", r"\b(windows?|shade|blind)\b",
    [
        Api("close_all_windows", "close every window", _close_all,
            writes=tuple(c.window(s) for s in c.DOOR_SIDES),
            patterns=(r"\bclose\s+(all\s+(of\s+)?(the\s+)?|every\s+)windows?\b",)),
        Api("set_rear_shade", "raise or lower the rear sunshade", _rear_shade,
            params={"value": p_value("shade", "0 down, 100 up", 0, 100)}, required=("value",),
            writes=(c.REAR_SHADE,),
            patterns=(r"\b(rear|back)\s+(sun\s*)?(shade|blind)\b",)),
        Api("set_window", "open, close or vent a window", _set_window,
            params={"zone": p_zone(ZONES, "driver"),
                    "value": p_value("opening", "0 closed, 100 fully open", 0, 100)},
            required=("value",), writes=tuple(c.window(s) for s in c.DOOR_SIDES),
            patterns=(r"^(?!.*\b(defrost|demist|defog))(?=.*\bwindows?\b)",)),
    ],
)
