"""Real GPS over NMEA 0183 (USB serial) as a CabinEnv position source.

Callers: nova_hailo.web.app (reader thread next to the simulated drive), tests.

With a fix, position, heading and speed are written as source ``gps``. The
store refuses ``sim`` writes over a real source, so every tool, the planner and
the displays see the real position. While the simulated drive plays a route it
owns position and speed instead (the map puck and the speedometer stay in step);
when it parks, the next sentence brings the real position back. Without a
fix (indoors, unplugged) those paths go back to ``sim`` and keep their last
values, and the simulated drive takes over from there. ``Location.Place`` is
reverse-geocoded from the fix (Mapbox, throttled) and keeps its last value when
a lookup fails.

The feeder is the only owner of the serial port.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.drive import safe_error

logger = logging.getLogger(__name__)

KNOT_KPH = 1.852
PlaceLookup = Callable[[float, float], "str | None"]
# Paths a fix owns; the simulation gets them back when the fix is lost (all of
# them) or while it plays a route (the motion ones).
_MOTION = (c.LAT, c.LON, c.HEADING, c.SPEED)
_OWNED = (*_MOTION, c.PLACE)


def nmea_checksum_ok(line: str) -> bool:
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cs = line[1:].partition("*")
    try:
        want = int(cs[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _coord(value: str, hemi: str, deg_digits: int) -> float | None:
    """NMEA ddmm.mmmm (latitude) or dddmm.mmmm (longitude) to signed degrees."""
    if not value or hemi not in ("N", "S", "E", "W"):
        return None
    try:
        deg = int(value[:deg_digits])
        minutes = float(value[deg_digits:])
    except ValueError:
        return None
    out = deg + minutes / 60.0
    return round(-out if hemi in ("S", "W") else out, 6)


def parse_nmea(line: str) -> dict[str, Any] | None:
    """RMC, GGA or VTG from any talker (GP, GN, GL, ...); None for anything else or a bad line."""
    if not nmea_checksum_ok(line):
        return None
    f = line.strip()[1:].split("*", 1)[0].split(",")
    kind = f[0][2:]
    if kind == "RMC" and len(f) >= 9:
        knots = _float(f[7])
        return {"type": "RMC", "valid": f[2] == "A",
                "lat": _coord(f[3], f[4], 2), "lon": _coord(f[5], f[6], 3),
                "speed_kph": None if knots is None else round(knots * KNOT_KPH, 2),
                "course": _float(f[8])}
    if kind == "GGA" and len(f) >= 8:
        quality, sats = _float(f[6]), _float(f[7])
        return {"type": "GGA", "quality": int(quality or 0), "sats": int(sats or 0),
                "lat": _coord(f[2], f[3], 2), "lon": _coord(f[4], f[5], 3)}
    if kind == "VTG" and len(f) >= 8:
        return {"type": "VTG", "course": _float(f[1]), "speed_kph": _float(f[7])}
    return None


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def place_name(payload: dict[str, Any]) -> str | None:
    """'Area, City' from a Mapbox Geocoding v6 reverse answer, whatever the top feature's type."""
    features = payload.get("features") or []
    if not features:
        return None
    props = features[0].get("properties") or {}
    ctx = props.get("context") or {}

    def name(kind: str) -> str:
        return str((ctx.get(kind) or {}).get("name") or "").strip()

    parts: list[str] = []
    for part in (name("neighborhood") or name("locality"), name("place") or name("region")):
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts) or props.get("place_formatted") or props.get("name") or None


def mapbox_place_lookup(token: str, timeout_s: float = 3.0) -> PlaceLookup:
    """Mapbox reverse geocoding as a place lookup."""
    import httpx

    def lookup(lat: float, lon: float) -> str | None:
        r = httpx.get("https://api.mapbox.com/search/geocode/v6/reverse",
                      params={"longitude": lon, "latitude": lat, "limit": 1,
                              "access_token": token}, timeout=timeout_s)
        r.raise_for_status()
        return place_name(r.json())

    return lookup


def _open_serial(device: str, baud: int) -> Any:
    import serial

    return serial.Serial(device, baud, timeout=1.0)


class GpsFeeder:
    def __init__(
        self,
        cabin: Any,
        *,
        device: str = "/dev/ttyUSB0",
        baud: int = 9600,
        place_lookup: PlaceLookup | None = None,
        place_every_m: float = 500.0,
        place_every_s: float = 60.0,
        stale_s: float = 3.0,
        retry_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        opener: Callable[[str, int], Any] = _open_serial,
        simulating: Callable[[], bool] = lambda: False,
    ) -> None:
        self.cabin = cabin
        self.store = cabin.store
        self.device = device
        self.baud = int(baud)
        self.place_lookup = place_lookup
        self.place_every_m = float(place_every_m)
        self.place_every_s = float(place_every_s)
        self.stale_s = float(stale_s)
        self.retry_s = float(retry_s)
        self._clock = clock
        self._opener = opener
        self._simulating = simulating
        self.fixed = False
        self._last_fix_t = 0.0
        self._place_at: tuple[float, float] | None = None  # where the last lookup succeeded
        self._place_t: float | None = None                 # when the last lookup was tried
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def feed_line(self, line: str) -> None:
        msg = parse_nmea(line)
        if msg is None:
            return
        values: dict[str, Any] = {}
        has_pos = msg.get("lat") is not None and msg.get("lon") is not None
        if msg["type"] == "GGA":
            values[c.GPS_SATS] = msg["sats"]
            fixed = msg["quality"] >= 1 and has_pos
        elif msg["type"] == "RMC":
            fixed = msg["valid"] and has_pos
        else:  # VTG only adds speed and course to a fix it did not establish
            if not self.fixed:
                return
            fixed = True
        if not fixed:
            self._lose(values)
            return
        if self._simulating():
            self.store.release(_MOTION)
        else:
            if has_pos:
                values[c.LAT], values[c.LON] = msg["lat"], msg["lon"]
            if msg.get("speed_kph") is not None:
                values[c.SPEED] = min(300.0, max(0.0, float(msg["speed_kph"])))
            if msg.get("course") is not None:  # empty while standing still
                values[c.HEADING] = float(msg["course"]) % 360.0
        values[c.GPS_FIX] = True
        self.store.update(values, source="gps")
        self.fixed = True
        self._last_fix_t = self._clock()
        self._maybe_place()

    def check_stale(self) -> None:
        """Lose the fix when the receiver has gone quiet (unplugged, port error)."""
        if self.fixed and self._clock() - self._last_fix_t > self.stale_s:
            self._lose({c.GPS_SATS: 0})

    def _lose(self, values: dict[str, Any]) -> None:
        if self.fixed:
            self.store.release(_OWNED)
            self.fixed = False
        values[c.GPS_FIX] = False
        self.store.update(values, source="gps")

    def _maybe_place(self) -> None:
        if self.place_lookup is None:
            return
        here = (self.store.get(c.LAT), self.store.get(c.LON))
        now = self._clock()
        if self._place_t is not None:
            if now - self._place_t < self.place_every_s:
                return
            if self._place_at is not None and _distance_m(self._place_at, here) < self.place_every_m:
                return
        self._place_t = now
        try:
            name = self.place_lookup(*here)
        except Exception as exc:  # noqa: BLE001 — offline keeps the last place
            logger.info("gps: place lookup failed (%s)", safe_error(exc))
            name = None
        if name:
            self._place_at = here
            self.store.update({c.PLACE: str(name)}, source="gps")

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="cabingym-gps", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)

    def run(self) -> None:
        """Read the port until stopped; reopen it after errors without flooding the log."""
        warned = False
        while not self._stop.is_set():
            try:
                with self._opener(self.device, self.baud) as port:
                    logger.info("gps: reading %s at %d baud", self.device, self.baud)
                    warned = False
                    while not self._stop.is_set():
                        raw = port.readline()
                        if raw:
                            try:
                                self.feed_line(raw.decode("ascii", errors="replace"))
                            except Exception:  # noqa: BLE001 — one bad sentence never stops the feed
                                logger.debug("gps: sentence skipped", exc_info=True)
                        self.check_stale()
            except Exception as exc:  # noqa: BLE001 — no receiver means the sim drive, not a crash
                if not warned:
                    logger.warning("gps: %s unavailable (%s); retrying every %.0f s",
                                   self.device, exc, self.retry_s)
                    warned = True
            self.check_stale()
            self._stop.wait(self.retry_s)


def build_gps_feeder(
    cfg_get: Callable[..., Any],
    cabin: Any,
    *,
    simulating: Callable[[], bool] = lambda: False,
) -> GpsFeeder | None:
    """The feeder from config ``cabingym.gps``; None when it is off.

    ``gps``: ``enabled`` (bool), ``device`` (default /dev/ttyUSB0), ``baud`` (9600),
    ``place_lookup`` (default on; needs MAPBOX_API_KEY or MAPBOX_ACCESS_TOKEN).
    """
    conf = cfg_get("cabingym", "gps", default=None) or {}
    if not conf.get("enabled"):
        return None
    lookup = None
    if conf.get("place_lookup", True):
        token = (os.environ.get("MAPBOX_API_KEY") or os.environ.get("MAPBOX_ACCESS_TOKEN")
                 or "").strip()
        if token:
            lookup = mapbox_place_lookup(token)
    return GpsFeeder(cabin, device=str(conf.get("device") or "/dev/ttyUSB0"),
                     baud=int(conf.get("baud") or 9600), place_lookup=lookup,
                     simulating=simulating)
