# nova_hailo/cabingym/devices/hvac.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    ZONE_LABEL, Api, ApiContext, ApiResult, clamp, clarify, device, done, fmt_num,
    p_bool, p_delta, p_enum, p_value, p_zone, resolve_value,
)

ZONES = ("driver", "passenger", "all")


def _set_temperature(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    zone = args.get("zone", "driver")
    targets: dict[str, Any] = {}
    clamped = False
    for st in c.HVAC_ZONES[zone]:
        path = c.hvac_temp(st)
        want = round(resolve_value(args, ctx.snapshot[path]) * 2) / 2
        val = clamp(want, 16.0, 28.0)
        clamped = clamped or val != want
        targets[path] = val
    values = set(targets.values())
    if len(values) == 1:
        speak = f"{ZONE_LABEL[zone]} set to {fmt_num(next(iter(values)))} degrees."
    else:
        speak = "Both sides adjusted."
    if clamped:
        speak += " That's the limit."
    return done(speak, targets)


def _set_fan(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    zone = args.get("zone", "all")
    targets = {
        c.hvac_fan(st): int(clamp(resolve_value(args, ctx.snapshot[c.hvac_fan(st)]), 0, 6))
        for st in c.HVAC_ZONES[zone]
    }
    level = max(targets.values())
    if zone == "all" or zone not in ("driver", "passenger"):
        speak = "Fan off." if level == 0 else ("Fan on max." if level == 6 else f"Fan set to {level}.")
        return done(speak, targets)
    # One side: name it, and say so when the other side differs (the cockpit shows the higher).
    other = "passenger" if zone == "driver" else "driver"
    other_level = int(ctx.snapshot[c.hvac_fan(other.capitalize())])
    speak = f"{ZONE_LABEL[zone]} fan " + ("off." if level == 0 else f"set to {level}.")
    if other_level != level:
        speak += f" {ZONE_LABEL[other]} is still on {other_level}."
    return done(speak, targets)


def _flag(path: str, on_text: str, off_text: str):
    def handler(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
        on = bool(args.get("on", True))
        return done(on_text if on else off_text, {path: on})
    return handler


def _set_ac(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    """The A/C is one switch for the cabin. Asked for one side, say so rather
    than switch it for everyone: "passenger AC off" turned off the driver's too."""
    zone = args.get("zone", "all")
    on = bool(args.get("on", True))
    if zone != "all":
        verb = "on" if on else "off"
        return clarify(f"The A/C is for the whole cabin, not one side. Turn it {verb} for everyone, "
                       f"or change the {ZONE_LABEL[zone].lower()} temperature instead?")
    return done("Air conditioning on." if on else "Air conditioning off.", {c.AC: on})


def _set_defrost(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    rear = args.get("target", "front") == "rear"
    on = bool(args.get("on", True))
    path = c.DEFROST_REAR if rear else c.DEFROST_FRONT
    return done(f"{'Rear' if rear else 'Front'} defroster {'on' if on else 'off'}.", {path: on})


def _sync_zones(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    temp = ctx.snapshot[c.hvac_temp("Driver")]
    return done(f"Passenger side synced to {fmt_num(temp)} degrees.",
                {c.hvac_temp("Passenger"): temp, c.HVAC_SYNC: True})


DEVICE = device(
    "hvac",
    "climate control: temperature, fan, A/C, defrost and recirculation",
    r"\b(temp\w*|degrees|warm\w*|cool\w*|cold|hot|heat|fan|a\.?c\.?|air\s*con\w*|defrost\w*|"
    r"recirc\w*|climate|fresh\s+air)\b",
    [
        Api("set_defrost", "turn the front or rear defroster on or off", _set_defrost,
            params={"target": p_enum(("front", "rear"), desc="Which defroster",
                                     synonyms={"front": ("windshield", "windscreen"),
                                               "rear": ("back window", "back glass")},
                                     default="front"),
                    "on": p_bool("Defroster on", default=True)},
            writes=(c.DEFROST_FRONT, c.DEFROST_REAR),
            patterns=(r"\b(defrost(er)?|demist(er)?|defog(ger)?)\b",)),
        Api("set_recirculation", "switch between recirculated and fresh air",
            _flag(c.RECIRC, "Recirculation on.", "Fresh air on."),
            params={"on": p_bool("Recirculation on", parser="recirc", default=True)},
            writes=(c.RECIRC,), patterns=(r"\b(recirc\w*|fresh\s+air|outside\s+air)\b",)),
        Api("set_auto", "turn automatic climate control on or off",
            _flag(c.HVAC_AUTO, "Auto climate on.", "Auto climate off."),
            params={"on": p_bool("Auto climate on", default=True)}, writes=(c.HVAC_AUTO,),
            patterns=(r"\b(auto|automatic)\s+(climate|mode|ac|a/c)\b|\bclimate\s+(to\s+)?auto\b",)),
        Api("sync_zones", "match the passenger temperature to the driver's", _sync_zones,
            writes=(c.hvac_temp("Passenger"), c.HVAC_SYNC),
            patterns=(r"\b(sync|synchroni[sz]e|match)\b.*\b(zones?|sides?|temp\w*|climate|passenger)\b",)),
        Api("set_ac", "turn the air conditioning on or off (one switch for the whole cabin)",
            _set_ac,
            params={"on": p_bool("A/C on", default=True), "zone": p_zone(ZONES, "all")},
            writes=(c.AC,),
            patterns=(r"\b(a\.?c\.?|a/c|air\s*con(ditioning)?)\b",)),
        Api("set_fan", "change the fan speed", _set_fan,
            params={"zone": p_zone(ZONES, "all"),
                    "value": p_value("fan_level", "Fan level 0-6 (0 off, 6 max)", 0, 6),
                    "delta": p_delta("fan_level", "Change in fan level")},
            requires_any=("value", "delta"),
            writes=(c.hvac_fan("Driver"), c.hvac_fan("Passenger")),
            patterns=(r"\b(fan|blower|air\s*flow)\b",)),
        Api("set_temperature", "set or change the cabin temperature", _set_temperature,
            params={"zone": p_zone(ZONES, "driver"),
                    "value": p_value("temperature", "Target temperature in Celsius", 16, 28),
                    "delta": p_delta("temperature", "Change in degrees Celsius")},
            requires_any=("value", "delta"),
            writes=(c.hvac_temp("Driver"), c.hvac_temp("Passenger")),
            patterns=(
                r"^(?!\s*(what'?s|what|how|is|are|tell)\b).*\b(temperature|temp|degrees|warmer|"
                r"cooler|colder|hotter|heat\s+(it\s+)?up|cool\s+(it\s+)?down|too\s+(hot|cold|warm)|"
                r"(i'?m|it'?s|feeling)\s+(a\s+(bit|little)\s+)?(cold|hot|freezing|boiling|chilly|warm))\b",
            )),
    ],
)
