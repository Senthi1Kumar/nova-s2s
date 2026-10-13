"""Touchscreen writes and the event policy for changes Nova did not make.

Callers: nova_hailo.web.app (POST /v1/vehicle/set), tests.

A touch on the cockpit UI is applied exactly like a voice command: it becomes a
device API call and runs through the VehicleExecutor and its safety gate, so
the screen can do nothing the gate would refuse to a voice request. The event
policy then decides whether Nova says anything: most touches are silent
context (kept as recent cabin events the backend planner sees); a rule hit,
such as setting the driver temperature to 19 °C or below, is announced, with
an offer the driver can accept by saying "yes".
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall

COLD_C = 19.0
OFFER_TTL_S = 45.0
DRIVER_TEMP = c.hvac_temp("Driver")


def _zone_call(device: str, api: str, zone: str) -> Callable[[Any], tuple[str, str, dict]]:
    return lambda v: (device, api, {"zone": zone, "value": float(v)})


def _value_call(device: str, api: str) -> Callable[[Any], tuple[str, str, dict]]:
    return lambda v: (device, api, {"value": float(v)})


def _lock_call(v: Any) -> tuple[str, str, dict]:
    return ("doors", "lock_doors", {"locked": bool(v)})


_CONTROLS: dict[str, tuple[str, Callable[[Any], tuple[str, str, dict]]]] = {}


def _control(alias: str, label: str, fn: Callable[[Any], tuple[str, str, dict]]) -> None:
    _CONTROLS[c.resolve(alias)] = (label, fn)


_control("hvac.driver.temp", "driver temperature", _zone_call("hvac", "set_temperature", "driver"))
_control("hvac.passenger.temp", "passenger temperature",
         _zone_call("hvac", "set_temperature", "passenger"))
_control("hvac.driver.fan", "driver fan", _zone_call("hvac", "set_fan", "driver"))
_control("hvac.passenger.fan", "passenger fan", _zone_call("hvac", "set_fan", "passenger"))
_control("seat.driver.heat", "driver seat heating", _zone_call("seats", "set_seat_heating", "driver"))
_control("seat.passenger.heat", "passenger seat heating",
         _zone_call("seats", "set_seat_heating", "passenger"))
for _zone in ("driver", "passenger", "rear_left", "rear_right"):
    _control(f"window.{_zone}", f"{_zone.replace('_', ' ')} window",
             _zone_call("windows", "set_window", _zone))
    _control(f"lock.{_zone}", "door locks", _lock_call)
_control("sunroof", "sunroof", _value_call("body", "set_sunroof"))
_control("sunshade", "sunshade", _value_call("body", "set_sunshade"))
_control("volume", "volume", _value_call("media", "set_volume"))


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class EventPolicy:
    """What Nova does about a change it did not make: stay silent, or announce."""

    def __init__(
        self,
        cabin: CabinEnv,
        executor: Any,
        *,
        announce: Callable[[str], None],
        keep: int = 5,
    ) -> None:
        self.cabin = cabin
        self.executor = executor
        self.announce = announce
        cabin.recent_events = deque(maxlen=keep)  # type: ignore[attr-defined]

    def on_hmi_write(self, path: str, before: Any, after: Any) -> str:
        label = _CONTROLS.get(path, (path.rsplit(".", 1)[-1], None))[0]
        self.cabin.recent_events.append(  # type: ignore[attr-defined]
            f"On the touchscreen, the driver set {label} to {_fmt(after)}.")
        if path == DRIVER_TEMP and float(after) <= COLD_C < float(before):
            n = _fmt(after)
            seat = c.resolve("seat.driver.heat")
            if self.cabin.store.get(seat) == 0:
                self.executor.offer(
                    [VehicleCall("seats", "set_seat_heating", {"zone": "driver", "value": 2.0},
                                 {"zone": "default", "value": "default"})],
                    ttl_s=OFFER_TTL_S, trace={"router": "policy", "rule": "cold_cabin"})
                self.announce(f"You've set it to {n} degrees, that's quite cool. "
                              "Want me to turn on your seat heating?")
            else:
                self.announce(f"You've set it to {n} degrees, that's on the cool side.")
            return "announce"
        return "silent"


def hmi_set(
    cabin: CabinEnv,
    executor: Any,
    body: dict[str, Any],
    *,
    policy: EventPolicy | None = None,
) -> dict[str, Any]:
    """Apply one touchscreen write; returns {ok, status, speak, error?, trace}."""
    path = body.get("path") or (c.resolve(str(body["alias"])) if body.get("alias") else None)
    if not path:
        return {"ok": False, "error": "missing_path"}
    if "value" not in body:
        return {"ok": False, "error": "missing_value"}
    try:
        signal = cabin.store.signal(path)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "unknown_path"}
    if path not in _CONTROLS:
        return {"ok": False, "error": "not_writable" if signal.kind != "actuator" else "unsupported"}
    _label, build = _CONTROLS[path]
    try:
        device, api, args = build(body["value"])
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_value"}
    before = cabin.store.get(path)
    outcome = RouteOutcome(
        "vehicle", [VehicleCall(device, api, args, {k: "default" for k in args})],
        trace={"router": "hmi"},
    )
    result = executor.run(outcome)
    status = str(result.get("status") or "")
    trace = (result.get("result") or {}).get("trace") or {}
    out: dict[str, Any] = {"ok": status == "success", "status": status,
                           "speak": result.get("speak") or "", "trace": trace}
    if status == "success":
        if policy is not None:
            policy.on_hmi_write(path, before, cabin.store.get(path))
        return out
    if status == "confirm":
        executor.drop_pending()  # a touch is not a voice turn; the screen asks again itself
        out["error"] = "needs_confirmation"
    else:
        out["error"] = status or "error"
    return out
