# nova_hailo/cabingym/devices/seats.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clamp, device, done, p_bool, p_delta, p_value, p_zone,
    resolve_value,
)

ZONES = ("driver", "passenger", "all")
SEAT_LABEL = {"driver": "Driver seat", "passenger": "Passenger seat", "all": "Front seat"}
LEVEL_WORD = {1: "low", 2: "medium", 3: "high"}


def _level(leaf: str, label: str):
    def handler(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
        zone = args.get("zone", "driver")
        targets = {
            c.seat(side, leaf): int(clamp(resolve_value(args, ctx.snapshot[c.seat(side, leaf)]), 0, 3))
            for side in c.SEAT_SIDES[zone]
        }
        level = max(targets.values())
        if level == 0:
            return done(f"{SEAT_LABEL[zone]} {label} off.", targets)
        return done(f"{SEAT_LABEL[zone]} {label} on {LEVEL_WORD[level]}.", targets)
    return handler


def _massage(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    zone = args.get("zone", "driver")
    on = bool(args.get("on", True))
    targets = {c.seat(side, "IsMassageActive"): on for side in c.SEAT_SIDES[zone]}
    return done(f"{SEAT_LABEL[zone]} massage {'on' if on else 'off'}.", targets)


_Z = r"(rear\s+|back\s+|front\s+|passenger\s+)?"
DEVICE = device(
    "seats", "seat heating, ventilation and massage", r"\b(seats?|massag\w*)\b",
    [
        Api("set_seat_heating", "heat a seat", _level("Heating", "heating"),
            params={"zone": p_zone(ZONES, "driver"),
                    "value": p_value("seat_level", "Heating level 0-3", 0, 3),
                    "delta": p_delta("seat_level", "Change in heating level")},
            requires_any=("value", "delta"),
            writes=tuple(c.seat(s, "Heating") for s in c.SEAT_SIDES["all"]),
            patterns=(rf"\b(seat\s+heat(er|ing)?|heat(ed)?\s+(my\s+|the\s+)?{_Z}seats?|"
                      rf"warm\s+(up\s+)?(my\s+|the\s+)?{_Z}seats?|seat\s+warmer)\b",)),
        Api("set_seat_ventilation", "cool or ventilate a seat", _level("Ventilation", "ventilation"),
            params={"zone": p_zone(ZONES, "driver"),
                    "value": p_value("seat_level", "Ventilation level 0-3", 0, 3),
                    "delta": p_delta("seat_level", "Change in ventilation level")},
            requires_any=("value", "delta"),
            writes=tuple(c.seat(s, "Ventilation") for s in c.SEAT_SIDES["all"]),
            patterns=(rf"\b(seat\s+(ventilation|cooling|vent|fan)|ventilat(e|ed)\s+(my\s+|the\s+)?{_Z}seats?|"
                      rf"cool(ed)?\s+(my\s+|the\s+)?{_Z}seats?)\b",)),
        Api("set_massage", "turn seat massage on or off", _massage,
            params={"zone": p_zone(ZONES, "driver"), "on": p_bool("Massage on", default=True)},
            writes=tuple(c.seat(s, "IsMassageActive") for s in c.SEAT_SIDES["all"]),
            patterns=(r"\bmassag(e|er|ing)\b",)),
    ],
)
