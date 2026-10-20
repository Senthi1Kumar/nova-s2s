# nova_hailo/cabingym/runtime.py
"""One vehicle: store + adapter + nav provider + devices.

Callers: cabingym.executor, .env, .router_*, nova_hailo.tools.oem_tools.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.adapters.base import VehicleAdapter
from nova_hailo.cabingym.adapters.sim import InProcessSim
from nova_hailo.cabingym.devices import build_devices
from nova_hailo.cabingym.devices.base import ApiContext
from nova_hailo.cabingym.nav import FixtureNav, NavProvider

logger = logging.getLogger(__name__)

# Devices the cockpit can show and a driver can ask for by voice.
# Navigation stays on the Mapbox path. Drive (speed, gear, location) stays
# on the cloud turn so a place question is not taken as a cabin command.
CABIN_CONTROL = frozenset({
    "hvac", "lights", "media", "seats", "windows", "doors", "body", "cabin", "system", "phone",
})



def parse_places(conf: Any) -> dict[str, tuple[float, float]]:
    """``{name: [lat, lon] | {lat, lon}}`` from config; entries that are not a valid
    coordinate are logged and skipped."""
    out: dict[str, tuple[float, float]] = {}
    for name, value in (conf or {}).items() if isinstance(conf, dict) else ():
        try:
            lat, lon = (value["lat"], value["lon"]) if isinstance(value, dict) else value
            lat, lon = float(lat), float(lon)
        except (KeyError, TypeError, ValueError):
            logger.warning("cabingym: place %r is not [lat, lon]; skipped", name)
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            logger.warning("cabingym: place %r is out of range; skipped", name)
            continue
        out[str(name)] = (lat, lon)
    return out


ROUTE_AHEAD_POINTS = 60


def route_ahead(points: list[tuple[float, float]], lat: float, lon: float,
                max_points: int = ROUTE_AHEAD_POINTS) -> tuple[tuple[float, float], ...]:
    """The planned road from the point nearest the car to its end, thinned to
    ``max_points`` (a search request carries it in the URL)."""
    if len(points) < 2:
        return ()
    near = min(range(len(points)), key=lambda i: (points[i][0] - lat) ** 2 + (points[i][1] - lon) ** 2)
    ahead = points[near:]
    if len(ahead) < 2:
        return ()
    if len(ahead) > max_points:
        step = (len(ahead) - 1) / (max_points - 1)
        ahead = [ahead[round(i * step)] for i in range(max_points)]
    return tuple((float(a), float(b)) for a, b in ahead)


class CabinEnv:
    def __init__(
        self,
        profile: str = "ev_demo",
        *,
        adapter: str | VehicleAdapter = "sim",
        nav: NavProvider | None = None,
        live: bool = False,
        ramp: bool = False,
    ):
        self.profile = c.get_profile(profile)
        self.store = c.build_store(profile)
        if isinstance(adapter, str):
            if adapter != "sim":
                raise ValueError(f"cabingym adapter {adapter!r} is not available yet; use 'sim'")
            adapter = InProcessSim(ramp=ramp)
        self.adapter = adapter
        self.adapter.attach(self.store)
        self.nav: NavProvider = nav if nav is not None else FixtureNav()
        self.devices = build_devices()
        self.live = live
        # Last cabin command the driver was in the middle of, and the recent
        # user/assistant pairs for this turn. Jev reads both. Code uses them
        # when a follow-up names a value but not a zone.
        self.focus: dict[str, Any] | None = None
        self.dialog: list[dict[str, str]] = []
        # The last few cabin changes (driver-facing actuators only), newest last,
        # for the agent's per-turn context note.
        self.recent_changes: deque[tuple[str, Any]] = deque(maxlen=3)
        # The planned road as (lat, lon), set by the drive feed; empty when not navigating.
        self.route_points: list[tuple[float, float]] = []
        self.store.subscribe(self._note_changes)

    def _note_changes(self, changes: list[Any]) -> None:
        for ch in changes:
            sig = self.store.signal(ch.path)
            if sig.kind == "actuator" and sig.scored:
                self.recent_changes.append((ch.path, ch.after))

    @property
    def hybrid(self) -> bool:
        return self.adapter.name != "sim"

    @property
    def allow_poi_search(self) -> bool:
        """Fixture POIs are fine in the gym but must never be spoken as real places."""
        return bool(self.nav.is_live) or not self.live

    def context(self) -> ApiContext:
        snap = self.store.snapshot()
        return ApiContext(snap, self.nav, self.profile.powertrain, self.hybrid,
                          route_ahead=route_ahead(self.route_points, snap[c.LAT], snap[c.LON]))

    def configure_site(self, places: Any = None, *, area: str | None = None) -> None:
        """Live site from config: saved places by name (``start`` is where the simulated
        car begins) and the area name shown before a GPS fix. Bad entries are skipped."""
        points = parse_places(places)
        writes: dict[str, Any] = {}
        if points:
            saved = {str(p["name"]): dict(p) for p in self.store.get(c.NAV_SAVED)}
            for name, (lat, lon) in points.items():
                if name != "start":
                    saved[name] = {"name": name, "lat": lat, "lon": lon}
            writes[c.NAV_SAVED] = tuple(saved.values())
            if "start" in points:
                writes[c.LAT], writes[c.LON] = points["start"]
        if area:
            writes[c.PLACE] = str(area)
        if writes:
            self.store.apply(writes)

    def summary(self) -> dict[str, Any]:
        snap = self.store.snapshot()
        return {
            "speed_kph": snap[c.SPEED], "gear": snap[c.GEAR], "call_state": snap[c.CALL_STATE],
            "nav_active": snap[c.NAV_ACTIVE], "media_state": snap[c.MEDIA_STATE],
        }

    def observe(self) -> dict[str, Any]:
        """Cockpit VSS slice. Named fields so a judgment can point at one of them."""
        snap = self.store.snapshot()
        return {
            "hvac": {
                "driver_c": snap[c.hvac_temp("Driver")],
                "passenger_c": snap[c.hvac_temp("Passenger")],
                "driver_fan": snap[c.hvac_fan("Driver")],
                "passenger_fan": snap[c.hvac_fan("Passenger")],
                "ac_on": bool(snap[c.AC]),
                "defrost_front": bool(snap[c.DEFROST_FRONT]),
                "defrost_rear": bool(snap[c.DEFROST_REAR]),
            },
            "lights": {
                "ambient_color": snap[c.AMBIENT_COLOR],
                "ambient_level": snap[c.AMBIENT_LEVEL],
                "headlights": snap[c.HEADLIGHTS],
            },
            "body": {
                "mirrors_folded": bool(snap[c.MIRRORS_FOLDED]),
                "sunroof": snap[c.SUNROOF],
                "wipers": snap[c.WIPERS],
                "locked": bool(snap[c.door("Row1.DriverSide", "IsLocked")]),
            },
            "media": {
                "state": snap[c.MEDIA_STATE],
                "title": snap[c.MEDIA_TITLE],
                "track": snap[c.MEDIA_TRACK],
                "volume": snap[c.VOLUME],
                "source": snap[c.MEDIA_SOURCE],
            },
            "seats": {"driver_heat": snap[c.seat("Row1.DriverSide", "Heating")]},
            "drive_mode": snap[c.DRIVE_MODE],
            "speed_kph": snap[c.SPEED],
            "gear": snap[c.GEAR],
        }

    def focused_zone(self, api_name: str, allowed: Any) -> str | None:
        """Zone from the previous cabin command, only for the same action."""
        focus = self.focus or {}
        if not focus.get("api") or focus.get("api") != api_name:
            return None
        zone = focus.get("zone")
        return zone if zone in allowed else None

    def dialog_zone(self, allowed: Any) -> tuple[str | None, frozenset[str]]:
        """The most recent zone the driver actually said, and the devices that turn was about
        (empty for a bare answer such as "passenger side please")."""
        from nova_hailo.cabingym.slots import parse_zone

        for turn in reversed(self.dialog or []):
            text = str(turn.get("user") or "")
            named, how = parse_zone(text, allowed, None)
            if how == "parser" and named in allowed:
                about = frozenset(name for name, d in self.devices.items()
                                  if d.keywords and re.search(d.keywords, text, re.I))
                return named, about
        return None, frozenset()

    def zone_from_dialog(self, allowed: Any, device: str | None = None) -> str | None:
        """Most recent zone the driver actually said, not a default. With a device, a zone
        said about another control does not count: "the seat on the driver side" is not
        the driver's fan."""
        zone, about = self.dialog_zone(allowed)
        if device is not None and about and device not in about:
            return None
        return zone

    def remember_turn(
        self, utterance: str, device: str | None, api: str | None,
        args: Any, status: str, speak: str,
    ) -> None:
        if device not in CABIN_CONTROL:
            return
        from nova_hailo.cabingym.slots import parse_zone

        args = dict(args or {})
        named, how = parse_zone(
            utterance, ("driver", "passenger", "all", "front", "rear", "rear_left", "rear_right"), None,
        )
        prev = self.focus or {}
        if how == "parser":
            zone = named
        elif isinstance(args.get("zone"), str):
            zone = args["zone"]
        elif prev.get("device") == device and prev.get("api") == (api or ""):
            zone = prev.get("zone")
        else:
            zone = None
        same = prev.get("device") == device
        color = args.get("color") if isinstance(args.get("color"), str) else (prev.get("color") if same else None)
        self.focus = {
            "utterance": (utterance or "")[:240],
            "device": device,
            "api": api or "",
            "zone": zone if isinstance(zone, str) else None,
            "color": color,
            "status": status,
            "speak": (speak or "")[:240],
        }
