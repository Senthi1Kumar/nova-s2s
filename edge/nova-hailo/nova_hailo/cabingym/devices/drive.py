# nova_hailo/cabingym/devices/drive.py
from __future__ import annotations

import logging
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import Api, ApiContext, ApiResult, device, done, p_enum
from nova_hailo.cabingym.nav import Place, haversine_km, mapbox_token

logger = logging.getLogger(__name__)

_CARDINAL = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
_GEAR = {"P": "park", "R": "reverse", "N": "neutral", "D": "drive"}
SIGNALS = ("speed", "gear", "odometer", "battery_voltage", "engine_temp", "fuel", "charge")


def _energy(ctx: ApiContext) -> str:
    if ctx.powertrain == "ice":
        return f"{round(ctx.snapshot[c.FUEL])} percent fuel"
    return f"{round(ctx.snapshot[c.SOC])} percent charge"


def _range(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    return done(f"About {round(ctx.snapshot[c.RANGE])} kilometres of range, {_energy(ctx)}.")


def _status(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    s, sig = ctx.snapshot, args["signal"]
    if sig == "speed":
        return done(f"You're doing {round(s[c.SPEED])} kilometres an hour.")
    if sig == "gear":
        return done(f"You're in {_GEAR[s[c.GEAR]]}.")
    if sig == "odometer":
        return done(f"The odometer reads {int(s[c.ODOMETER]):,} kilometres.")
    if sig == "battery_voltage":
        return done(f"The 12-volt battery is at {s[c.BATTERY_V]:.1f} volts.")
    if sig == "engine_temp":
        if ctx.powertrain != "ice":
            return done("This car has no engine.")
        return done(f"Coolant is at {round(s[c.ECT])} degrees.")
    if sig == "fuel" and ctx.powertrain != "ice":
        return done(f"This car is electric. The battery is at {round(s[c.SOC])} percent.")
    if sig == "charge" and ctx.powertrain == "ice":
        return done(f"This car runs on fuel. The tank is at {round(s[c.FUEL])} percent.")
    return done(f"You have {_energy(ctx)}.")


def reverse_place_name(lat: float, lon: float) -> str | None:
    """One Mapbox reverse lookup. None when there is no token or the request fails.

    The exception text is not logged: an HTTP error includes the token in the URL.
    """
    token = mapbox_token()
    if not token:
        return None
    try:
        from nova_hailo.cabingym.adapters.gps_nmea import mapbox_place_lookup

        return mapbox_place_lookup(token)(lat, lon)
    except Exception as exc:  # noqa: BLE001
        logger.info("drive:place_lookup_failed %s", type(exc).__name__)
        return None


def _location(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    here = Place("here", ctx.snapshot[c.LAT], ctx.snapshot[c.LON])
    for place in ctx.snapshot[c.NAV_SAVED]:
        if haversine_km(here, Place.from_dict(place)) <= 0.3:
            return done(f"You're at {place['name']}.")
    heading = _CARDINAL[int(((ctx.snapshot[c.HEADING] % 360) + 22.5) // 45) % 8]
    # The sim drive has an area name with no GPS fix. Speak that name. A fix is
    # not required; the cockpit chip stays "no fix" until a receiver says otherwise.
    area = str(ctx.snapshot.get(c.PLACE) or "").strip()
    if not area:
        area = (reverse_place_name(here.lat, here.lon) or "").strip()
        if area:
            return done(f"You're in {area}, heading {heading}.", {c.PLACE: area})
    if area:
        return done(f"You're in {area}, heading {heading}.")
    return done(f"You're at {here.lat:.4f}, {here.lon:.4f}, heading {heading}.")


DEVICE = device(
    "drive", "driving status: speed, gear, range, fuel or charge, and location",
    r"\b(range|speed|fast|gear|fuel|gas|charge|battery|odometer|mileage|location|where\s+am)\b",
    [
        Api("get_range", "tell the remaining range", _range, read_only=True,
            patterns=(r"\b(range|how\s+far\s+can\s+(i|we)\s+(go|drive|get)|(miles|kilomet(er|re)s?)\s+left|"
                      r"(battery|charge|fuel|gas)\s+(level|left)|how\s+much\s+(fuel|gas|charge|battery))\b",)),
        Api("get_vehicle_status", "tell a driving signal such as speed or gear", _status,
            params={"signal": p_enum(SIGNALS, desc="Which signal", synonyms={
                "speed": ("how fast",), "odometer": ("mileage",),
                "battery_voltage": ("battery voltage", "12 volt", "12v"),
                "engine_temp": ("coolant", "engine temp", "engine temperature"),
                "fuel": ("gas",), "charge": ("battery level",)})},
            required=("signal",), read_only=True,
            patterns=(r"\b(how\s+fast|my\s+speed|current\s+speed|which\s+gear|what\s+gear|"
                      r"battery\s+voltage|12\s*v(olt)?|coolant|engine\s+temp\w*|odometer|mileage)\b",)),
        Api("get_location", "tell where the car is", _location, read_only=True,
            patterns=(r"\bwhere\s+(am\s+i|are\s+we)\b|\b(my|our|current)\s+location\b",)),
    ],
)
