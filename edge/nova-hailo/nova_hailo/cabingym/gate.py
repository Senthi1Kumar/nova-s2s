# nova_hailo/cabingym/gate.py
"""Deterministic safety gate for vehicle calls. No model, no sampling.

Rules are ordered; the first match wins. G8 (no phone) runs first so a
disconnected phone refuses instead of asking for confirmation. Callers:
cabingym.executor.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.types import VehicleCall

Verdict = Literal["execute", "confirm_first", "refuse"]


@dataclass(frozen=True)
class GateVerdict:
    verdict: Verdict
    reason: str | None = None
    speak: str | None = None
    arg_overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason}


EXECUTE = GateVerdict("execute")

# How G10 names a control in its question.
_LABEL = {"hvac": "climate", "lights": "lights", "media": "music", "seats": "seats", "windows": "windows",
          "doors": "doors", "body": "sunroof or mirrors", "system": "car settings", "phone": "phone"}

# A destination or stop this far from the car is confirmed before the route changes.
FAR_PLACE_KM = 150.0


def _far_place(args: Mapping[str, Any], snapshot: Mapping[str, Any]) -> tuple[str, float] | None:
    from nova_hailo.cabingym.nav import Place, haversine_km

    lat, lon = snapshot.get(c.LAT), snapshot.get(c.LON)
    if lat is None or lon is None:
        return None
    here = Place("here", float(lat), float(lon))
    for p in [args.get("destination"), *(args.get("via") or ())]:
        if not isinstance(p, Mapping) or p.get("lat") is None or p.get("lon") is None:
            continue
        km = haversine_km(here, Place(str(p.get("name") or ""), float(p["lat"]), float(p["lon"])))
        if km > FAR_PLACE_KM:
            return str(p.get("name") or "That place"), km
    return None


def check(call: VehicleCall, snapshot: Mapping[str, Any]) -> GateVerdict:
    speed = float(snapshot.get(c.SPEED) or 0.0)
    moving = speed > 0.0
    key = (call.device, call.api)
    args = call.args
    if call.device == "phone" and not snapshot.get(c.PHONE_CONNECTED, False):
        return GateVerdict("refuse", "G8_no_phone", "No phone is connected.")
    if key == ("doors", "lock_doors") and args.get("locked") is False and speed > 5:
        return GateVerdict("refuse", "G1_unlock_moving", "I can't unlock the doors while we're moving.")
    if key == ("doors", "open_trunk") and speed > 5:
        return GateVerdict("refuse", "G1_trunk_moving", "I can't open the trunk while we're moving.")
    if key == ("body", "set_sunroof") and speed > 80 and float(args.get("value") or 0) != 0:
        return GateVerdict("confirm_first", "G2_sunroof_speed",
                           f"We're doing {round(speed)} kilometres an hour. Open the sunroof anyway?")
    if key == ("phone", "send_message"):
        return GateVerdict("confirm_first", "G3_message_readback",
                           f"Send to {args.get('recipient')}: \"{args.get('text')}\". Should I send it?")
    if key == ("phone", "read_messages") and moving:
        return GateVerdict("execute", "G4_read_while_driving", arg_overrides={"_truncate": True})
    if key == ("doors", "set_child_lock") and args.get("on") is False and moving:
        return GateVerdict("confirm_first", "G5_child_lock_moving",
                           "Turn off the child locks while we're moving?")
    if key == ("system", "set_drive_mode") and moving and snapshot.get(c.GEAR) != "P":
        return GateVerdict("confirm_first", "G6_drive_mode_moving",
                           f"Switch to {args.get('mode')} mode while driving?")
    if (call.grounding or {}).get("asked") == "no":
        return GateVerdict("confirm_first", "G10_unasked",
                           f"Change the {_LABEL.get(call.device, call.device)}? I didn't hear you ask for that.")
    if key in (("navigation", "navigate_to"), ("navigation", "add_stop")):
        far = _far_place(args, snapshot)
        if far is not None:
            name, km = far
            return GateVerdict("confirm_first", "G9_far_place",
                               f"{name} is about {km:,.0f} km away. Go there anyway?")
    if (key == ("system", "set_display_brightness") and args.get("value") is not None
            and float(args["value"]) < 10 and snapshot.get(c.HEADLIGHTS) in ("low", "high")):
        return GateVerdict("execute", "G7_brightness_floor", arg_overrides={"value": 10})
    return EXECUTE
