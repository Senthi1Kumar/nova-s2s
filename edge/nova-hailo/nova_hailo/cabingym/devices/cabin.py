# nova_hailo/cabingym/devices/cabin.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import Api, ApiContext, ApiResult, device, done, fmt_num

_SEAT_NAMES = {"Row1.DriverSide": "the driver's seat", "Row1.PassengerSide": "the front passenger seat",
               "Row2.DriverSide": "the rear left seat", "Row2.PassengerSide": "the rear right seat"}


def _half(value: float) -> str:
    return fmt_num(round(value * 2) / 2)


def _occupancy(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    seats = [name for side, name in _SEAT_NAMES.items() if ctx.snapshot[c.seat(side, "IsOccupied")]]
    if seats == ["the driver's seat"]:
        return done("Just you, in the driver's seat.")
    return done(f"{len(seats)} people: {', '.join(seats)}.")


DEVICE = device(
    "cabin", "cabin conditions: inside and outside temperature and who is in the car",
    r"\b(inside|outside|cabin|in\s+here|people|passengers|occupan\w*)\b",
    [
        Api("get_outside_temperature", "tell the outside temperature", lambda ctx, args: done(
                f"It's {_half(ctx.snapshot[c.OUTSIDE_TEMP])} degrees outside."), read_only=True,
            patterns=(r"\b(temp\w*|hot|cold|warm|degrees)\b.*\b(outside|out\s+there|exterior)\b|"
                      r"\b(outside|exterior)\s+(temp\w*|air)\b",)),
        Api("get_cabin_temperature", "tell the inside temperature", lambda ctx, args: done(
                f"It's {_half(ctx.snapshot[c.CABIN_TEMP])} degrees inside."), read_only=True,
            patterns=(r"\b(what('?s|\s+is)|how)\b.*\b(temp\w*|hot|cold|warm)\b.*\b(inside|in\s+here|cabin|"
                      r"in\s+the\s+car)\b|\bcabin\s+temp\w*",)),
        Api("get_occupancy", "tell who is in the car", _occupancy, read_only=True,
            patterns=(r"\bhow\s+many\s+(people|passengers)\b|\bwho('?s|\s+is)\s+(in|sitting)\b|"
                      r"\bis\s+(anyone|someone)\s+(in|sitting)\b|\boccupan\w+",)),
    ],
)
