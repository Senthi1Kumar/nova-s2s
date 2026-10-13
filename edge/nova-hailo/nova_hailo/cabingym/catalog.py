# nova_hailo/cabingym/catalog.py
"""VSS signal catalog, vehicle profiles and task aliases for CabinGym.

COVESA VSS 4.x paths where they exist; ``Vehicle.Private.Nova.*`` fills gaps
(phone, media session, nav session, light and wiper modes). Callers:
cabingym.runtime, .devices, .env, .scoring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from nova_hailo.cabingym.timezone import track_timezone
from nova_hailo.cabingym.vss import Signal, VssStore

C = "Vehicle.Cabin."
P = "Vehicle.Private.Nova."
EV = frozenset({"ev"})
ICE = frozenset({"ice"})

HVAC_ZONES: dict[str, tuple[str, ...]] = {
    "driver": ("Driver",), "passenger": ("Passenger",), "all": ("Driver", "Passenger"),
}
SEAT_SIDES: dict[str, tuple[str, ...]] = {
    "driver": ("Row1.DriverSide",), "passenger": ("Row1.PassengerSide",),
    "all": ("Row1.DriverSide", "Row1.PassengerSide"),
}
DOOR_SIDES: tuple[str, ...] = (
    "Row1.DriverSide", "Row1.PassengerSide", "Row2.DriverSide", "Row2.PassengerSide",
)
WINDOW_SIDES: dict[str, tuple[str, ...]] = {
    "driver": ("Row1.DriverSide",), "passenger": ("Row1.PassengerSide",),
    "rear_left": ("Row2.DriverSide",), "rear_right": ("Row2.PassengerSide",),
    "front": ("Row1.DriverSide", "Row1.PassengerSide"),
    "rear": ("Row2.DriverSide", "Row2.PassengerSide"),
    "all": DOOR_SIDES,
}


def hvac_temp(station: str) -> str:
    return f"{C}HVAC.Station.Row1.{station}.Temperature"


def hvac_fan(station: str) -> str:
    return f"{C}HVAC.Station.Row1.{station}.FanSpeed"


def seat(side: str, leaf: str) -> str:
    return f"{C}Seat.{side}.{leaf}"


def window(side: str) -> str:
    return f"{C}Door.{side}.Window.Position"


def door(side: str, leaf: str) -> str:
    return f"{C}Door.{side}.{leaf}"


AC = f"{C}HVAC.IsAirConditioningActive"
RECIRC = f"{C}HVAC.IsRecirculationActive"
DEFROST_FRONT = f"{C}HVAC.IsFrontDefrosterActive"
DEFROST_REAR = f"{C}HVAC.IsRearDefrosterActive"
HVAC_AUTO = f"{P}HVAC.IsAutoActive"
HVAC_SYNC = f"{P}HVAC.IsZoneSync"
CABIN_TEMP = f"{C}HVAC.AmbientAirTemperature"
REAR_SHADE = f"{C}RearShade.Position"
TRUNK = "Vehicle.Body.Trunk.Rear.IsOpen"
HEADLIGHTS = f"{P}Lights.HeadlightMode"
HAZARDS = "Vehicle.Body.Lights.Hazard.IsSignaling"
DOME = f"{C}Light.IsDomeOn"
REAR_READING = f"{P}Light.IsRearReadingOn"
AMBIENT_COLOR = f"{P}Light.Ambient.Color"
AMBIENT_LEVEL = f"{P}Light.Ambient.Intensity"
SUNROOF = f"{C}Sunroof.Position"
SUNSHADE = f"{C}Sunroof.Shade.Position"
WIPERS = f"{P}Wipers.Mode"
MIRRORS_FOLDED = f"{P}Mirrors.IsFolded"
MEDIA_STATE = f"{P}Media.State"
MEDIA_SOURCE = f"{P}Media.Source"
MEDIA_TITLE = f"{P}Media.Title"
MEDIA_TRACK = f"{P}Media.TrackIndex"
VOLUME = f"{C}Infotainment.Media.Volume"
NAV_ACTIVE = f"{P}Nav.Active"
NAV_DEST = f"{P}Nav.DestinationName"
NAV_ETA = f"{P}Nav.EtaMin"
NAV_DIST = f"{P}Nav.DistanceKm"
NAV_STOPS = f"{P}Nav.Stops"
NAV_SAVED = f"{P}Nav.SavedPlaces"
NAV_RESULTS = f"{P}Nav.LastResults"
NAV_DEST_LAT = f"{C}Infotainment.Navigation.DestinationSet.Latitude"
NAV_DEST_LON = f"{C}Infotainment.Navigation.DestinationSet.Longitude"
PHONE_CONNECTED = f"{P}Phone.Connected"
CALL_STATE = f"{P}Phone.CallState"
CALL_PEER = f"{P}Phone.CallPeer"
CALL_MUTED = f"{P}Phone.CallMuted"
CONTACTS = f"{P}Phone.Contacts"
MESSAGES = f"{P}Phone.Messages"
READ_IDS = f"{P}Phone.ReadIds"
OUTBOX = f"{P}Phone.Outbox"
MISSED = f"{P}Phone.MissedCalls"
LAST_DIALED = f"{P}Phone.LastDialed"
SPEED = "Vehicle.Speed"
GEAR = "Vehicle.Powertrain.Transmission.SelectedGear"
RANGE = "Vehicle.Powertrain.Range"
SOC = "Vehicle.Powertrain.TractionBattery.StateOfCharge.Displayed"
FUEL = "Vehicle.Powertrain.FuelSystem.RelativeLevel"
RPM = "Vehicle.Powertrain.CombustionEngine.Speed"
ECT = "Vehicle.Powertrain.CombustionEngine.ECT"
LAT = "Vehicle.CurrentLocation.Latitude"
LON = "Vehicle.CurrentLocation.Longitude"
HEADING = "Vehicle.CurrentLocation.Heading"
GPS_FIX = f"{P}Location.GpsFix"
GPS_SATS = f"{P}Location.Satellites"
PLACE = f"{P}Location.Place"
TIMEZONE = f"{P}Location.TimeZone"
SIM_AREA = "Palo Alto"
ODOMETER = "Vehicle.TraveledDistance"
BATTERY_V = "Vehicle.LowVoltageBattery.CurrentVoltage"
OUTSIDE_TEMP = "Vehicle.Exterior.AirTemperature"
CONNECTIVITY = f"{P}System.Connectivity"
BRIGHTNESS = f"{P}System.DisplayBrightness"
DRIVE_MODE = f"{P}System.DriveMode"

DEFAULT_CONTACTS: tuple[dict[str, Any], ...] = (
    {"name": "Sam", "number": "+1-555-0101",
     "place": {"name": "Sam's place", "lat": 37.4443, "lon": -122.1598}},
    {"name": "Priya", "number": "+1-555-0102"},
    {"name": "Alex", "number": "+1-555-0103"},
    {"name": "Mom", "number": "+1-555-0104"},
)
DEFAULT_MESSAGES: tuple[dict[str, Any], ...] = (
    {"id": "m1", "from": "Priya", "text": "Running ten minutes late, start without me.",
     "ts": "2026-09-28T08:05:00"},
    {"id": "m2", "from": "Sam", "text": "Can you pick up milk on the way home?",
     "ts": "2026-09-28T08:20:00"},
)
DEFAULT_MISSED: tuple[dict[str, Any], ...] = ({"from": "Alex", "ts": "2026-09-28T07:50:00"},)
DEFAULT_PLACES: tuple[dict[str, Any], ...] = (
    {"name": "home", "lat": 37.4275, "lon": -122.1697},
    {"name": "work", "lat": 37.4847, "lon": -122.1477},
)


def _act(path: str, dtype: str, default: Any, **kw: Any) -> Signal:
    return Signal(path, "actuator", dtype, default=default, **kw)  # type: ignore[arg-type]


def _sen(path: str, dtype: str, default: Any, **kw: Any) -> Signal:
    return Signal(path, "sensor", dtype, default=default, **kw)  # type: ignore[arg-type]


def _attr(path: str, default: Any) -> Signal:
    return Signal(path, "attribute", "list", default=default)


def _signals() -> tuple[Signal, ...]:
    out: list[Signal] = []
    for st in ("Driver", "Passenger"):
        out.append(_act(hvac_temp(st), "float", 21.0, unit="celsius", min=16, max=28))
        out.append(_act(hvac_fan(st), "int", 2, min=0, max=6, description="0 off, 1-5, 6 max"))
    out += [
        _act(AC, "bool", True), _act(RECIRC, "bool", False),
        _act(DEFROST_FRONT, "bool", False), _act(DEFROST_REAR, "bool", False),
        _act(HVAC_AUTO, "bool", True), _act(HVAC_SYNC, "bool", False),
        _sen(CABIN_TEMP, "float", 22.0, unit="celsius", min=-40, max=85),
    ]
    for side in SEAT_SIDES["all"]:
        out.append(_act(seat(side, "Heating"), "int", 0, min=0, max=3))
        out.append(_act(seat(side, "Ventilation"), "int", 0, min=0, max=3))
        out.append(_act(seat(side, "IsMassageActive"), "bool", False))
    for side in DOOR_SIDES:
        out.append(_sen(seat(side, "IsOccupied"), "bool", side == "Row1.DriverSide"))
        out.append(_act(window(side), "int", 0, unit="percent", min=0, max=100))
        out.append(_act(door(side, "IsLocked"), "bool", True))
        out.append(_sen(door(side, "IsOpen"), "bool", False))
    for side in WINDOW_SIDES["rear"]:
        out.append(_act(door(side, "IsChildLockActive"), "bool", False))
    out += [
        _act(REAR_SHADE, "int", 0, unit="percent", min=0, max=100),
        _act(TRUNK, "bool", False),
        _act(HEADLIGHTS, "enum", "auto", allowed=("off", "auto", "low", "high")),
        _act(HAZARDS, "bool", False), _act(DOME, "bool", False), _act(REAR_READING, "bool", False),
        _act(AMBIENT_COLOR, "enum", "white",
             allowed=("white", "blue", "red", "green", "purple", "orange", "pink")),
        _act(AMBIENT_LEVEL, "int", 50, unit="percent", min=0, max=100),
        _act(SUNROOF, "int", 0, unit="percent", min=-100, max=100,
             description="0 closed, 100 open, -100 tilted"),
        _act(SUNSHADE, "int", 0, unit="percent", min=0, max=100,
             description="0 retracted, 100 deployed"),
        _act(WIPERS, "enum", "off", allowed=("off", "auto", "low", "high")),
        _act(MIRRORS_FOLDED, "bool", False),
        _act(MEDIA_STATE, "enum", "stopped", allowed=("stopped", "playing", "paused")),
        _act(MEDIA_SOURCE, "enum", "streaming", allowed=("radio", "bluetooth", "usb", "streaming")),
        _act(MEDIA_TITLE, "str", ""),
        _act(MEDIA_TRACK, "int", 0, min=0, max=9999),
        _act(VOLUME, "int", 40, unit="percent", min=0, max=100),
        _act(NAV_ACTIVE, "bool", False), _act(NAV_DEST, "str", ""),
        _act(NAV_ETA, "float", 0.0, min=0, max=100000, scored=False),
        _act(NAV_DIST, "float", 0.0, min=0, max=100000, scored=False),
        _act(NAV_STOPS, "list", ()),
        _attr(NAV_SAVED, DEFAULT_PLACES),
        _act(NAV_RESULTS, "list", (), scored=False),
        _act(NAV_DEST_LAT, "float", 0.0, min=-90, max=90, scored=False),
        _act(NAV_DEST_LON, "float", 0.0, min=-180, max=180, scored=False),
        _sen(PHONE_CONNECTED, "bool", True),
        _act(CALL_STATE, "enum", "idle", allowed=("idle", "dialing", "ringing", "active")),
        _act(CALL_PEER, "str", ""), _act(CALL_MUTED, "bool", False),
        _attr(CONTACTS, DEFAULT_CONTACTS),
        _sen(MESSAGES, "list", DEFAULT_MESSAGES),
        _act(READ_IDS, "list", ()), _act(OUTBOX, "list", ()),
        _sen(MISSED, "list", DEFAULT_MISSED),
        _act(LAST_DIALED, "str", ""),
        _sen(SPEED, "float", 0.0, unit="km/h", min=0, max=300),
        _sen(GEAR, "enum", "P", allowed=("P", "R", "N", "D")),
        _sen(RANGE, "float", 210.0, unit="km", min=0, max=2000),
        _sen(SOC, "float", 68.0, unit="percent", min=0, max=100, profiles=EV),
        _sen(FUEL, "float", 55.0, unit="percent", min=0, max=100, profiles=ICE),
        _sen(RPM, "float", 0.0, unit="rpm", min=0, max=10000, profiles=ICE),
        _sen(ECT, "float", 20.0, unit="celsius", min=-40, max=150, profiles=ICE),
        _sen(LAT, "float", 37.4419, min=-90, max=90),
        _sen(LON, "float", -122.1430, min=-180, max=180),
        _sen(HEADING, "float", 0.0, min=0, max=360),
        _sen(GPS_FIX, "bool", False, scored=False, description="a real GPS has a fix"),
        _sen(GPS_SATS, "int", 0, min=0, max=64, scored=False),
        _sen(PLACE, "str", SIM_AREA, scored=False, description="area name for the position"),
        _sen(TIMEZONE, "str", "America/Los_Angeles", scored=False, description="IANA zone of the position"),
        _sen(ODOMETER, "float", 12000.0, unit="km", min=0, max=2000000),
        _sen(BATTERY_V, "float", 12.6, unit="V", min=0, max=20),
        _sen(OUTSIDE_TEMP, "float", 18.0, unit="celsius", min=-50, max=60),
        _sen(CONNECTIVITY, "enum", "4g", allowed=("none", "2g", "4g", "5g")),
        _act(BRIGHTNESS, "int", 70, unit="percent", min=0, max=100),
        _act(DRIVE_MODE, "enum", "comfort", allowed=("eco", "comfort", "sport")),
    ]
    return tuple(out)


SIGNALS: tuple[Signal, ...] = _signals()

_SIDE_ALIAS = {
    "driver": "Row1.DriverSide", "passenger": "Row1.PassengerSide",
    "rear_left": "Row2.DriverSide", "rear_right": "Row2.PassengerSide",
}
ALIASES: dict[str, str] = {
    "hvac.driver.temp": hvac_temp("Driver"), "hvac.passenger.temp": hvac_temp("Passenger"),
    "hvac.driver.fan": hvac_fan("Driver"), "hvac.passenger.fan": hvac_fan("Passenger"),
    "hvac.ac": AC, "hvac.recirc": RECIRC, "hvac.defrost.front": DEFROST_FRONT,
    "hvac.defrost.rear": DEFROST_REAR, "hvac.auto": HVAC_AUTO, "hvac.sync": HVAC_SYNC,
    "cabin.temp": CABIN_TEMP, "rear_shade": REAR_SHADE, "trunk": TRUNK,
    "headlights": HEADLIGHTS, "hazards": HAZARDS, "dome": DOME, "rear_reading": REAR_READING,
    "ambient.color": AMBIENT_COLOR, "ambient.level": AMBIENT_LEVEL, "sunroof": SUNROOF,
    "sunshade": SUNSHADE, "wipers": WIPERS, "mirrors.folded": MIRRORS_FOLDED,
    "media.state": MEDIA_STATE, "media.source": MEDIA_SOURCE, "media.title": MEDIA_TITLE,
    "media.track": MEDIA_TRACK, "volume": VOLUME, "nav.active": NAV_ACTIVE,
    "nav.dest": NAV_DEST, "nav.eta": NAV_ETA, "nav.dist": NAV_DIST, "nav.stops": NAV_STOPS,
    "nav.saved": NAV_SAVED, "nav.results": NAV_RESULTS, "nav.dest_lat": NAV_DEST_LAT,
    "nav.dest_lon": NAV_DEST_LON, "phone.connected": PHONE_CONNECTED,
    "call.state": CALL_STATE, "call.peer": CALL_PEER, "call.muted": CALL_MUTED,
    "contacts": CONTACTS, "messages": MESSAGES, "read_ids": READ_IDS, "outbox": OUTBOX,
    "missed": MISSED, "last_dialed": LAST_DIALED, "speed": SPEED, "gear": GEAR,
    "range": RANGE, "soc": SOC, "fuel": FUEL, "rpm": RPM, "ect": ECT, "lat": LAT,
    "lon": LON, "heading": HEADING, "gps.fix": GPS_FIX, "gps.sats": GPS_SATS,
    "place": PLACE, "timezone": TIMEZONE, "odometer": ODOMETER, "battery_v": BATTERY_V,
    "outside.temp": OUTSIDE_TEMP, "connectivity": CONNECTIVITY, "brightness": BRIGHTNESS,
    "drive_mode": DRIVE_MODE,
}
for _name, _side in _SIDE_ALIAS.items():
    ALIASES[f"window.{_name}"] = window(_side)
    ALIASES[f"lock.{_name}"] = door(_side, "IsLocked")
    ALIASES[f"door.{_name}.open"] = door(_side, "IsOpen")
    ALIASES[f"seat.{_name}.occupied"] = seat(_side, "IsOccupied")
for _name in ("driver", "passenger"):
    ALIASES[f"seat.{_name}.heat"] = seat(_SIDE_ALIAS[_name], "Heating")
    ALIASES[f"seat.{_name}.vent"] = seat(_SIDE_ALIAS[_name], "Ventilation")
    ALIASES[f"seat.{_name}.massage"] = seat(_SIDE_ALIAS[_name], "IsMassageActive")
for _name in ("rear_left", "rear_right"):
    ALIASES[f"child_lock.{_name}"] = door(_SIDE_ALIAS[_name], "IsChildLockActive")

_ALL_PATHS = frozenset(s.path for s in SIGNALS)


def resolve(name: str) -> str:
    """Alias or full VSS path → full path. KeyError on anything else."""
    if name in _ALL_PATHS:
        return name
    return ALIASES[name]


@dataclass(frozen=True)
class Profile:
    name: str
    powertrain: Literal["ev", "ice"]
    label: str
    overrides: dict[str, Any] = field(default_factory=dict)


PROFILES: dict[str, Profile] = {
    "ev_demo": Profile("ev_demo", "ev", "Demo EV", {RANGE: 210.0}),
    "audi_q7_ice": Profile("audi_q7_ice", "ice", "Audi Q7", {RANGE: 520.0}),
}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown cabingym profile {name!r}; choose from {sorted(PROFILES)}") from None


def signals_for(powertrain: str) -> list[Signal]:
    return [s for s in SIGNALS if powertrain in s.profiles]


def build_store(profile: str = "ev_demo") -> VssStore:
    prof = get_profile(profile)
    store = VssStore(signals_for(prof.powertrain))
    store.apply(prof.overrides)
    track_timezone(store, LAT, LON, TIMEZONE)
    return store


def scored_paths(store: VssStore) -> list[str]:
    """Actuator paths that count for F1/Acc (derived bookkeeping excluded)."""
    return [p for p in store.paths()
            if store.signal(p).kind == "actuator" and store.signal(p).scored]
