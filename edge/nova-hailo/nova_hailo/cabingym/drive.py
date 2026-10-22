"""Simulated drive along real road geometry, and the nova.nav feed that follows navigation.

Callers: nova_hailo.web.app (runs the drive loop next to the voice pipeline), tests.

``SimDrive`` moves the car along a route at a steady cruise speed and streams
speed, gear, position, heading, odometer and charge into the VSS store as
source ``sim``. The store refuses ``sim`` writes over real sources, so a GPS or
OBD feeder wins automatically. ``NavFeed`` watches the navigation signals the
navigation device writes: when a destination is set it routes from where the
car is, starts the drive and publishes a ``nova.nav`` event for displays; when
navigation stops or the car arrives, it parks the car and publishes again.
While a Directions plan is active it also publishes ``nova.nav.progress`` about
once a second, and holds at most one turn-by-turn line for a quiet moment.

Routing never runs inside a voice turn: store listeners only mark the feed
dirty, and the caller runs ``sync()`` off the event loop.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.vss import REAL_SOURCES

logger = logging.getLogger(__name__)

ROUTES_FILE = Path(__file__).parent / "routes" / "palo_alto.json"
LatLon = tuple[float, float]
_EARTH_KM = 6371.0
# Within this of a stop (or past its point on the route), the stop is done.
_STOP_REACHED_KM = 0.03
# With a real fix: further than this from the route is off it, and this long off it re-plans
# from where the car is (a GPS wobble or a parallel street is not a detour).
OFF_ROUTE_M = 60.0
OFF_ROUTE_REPLAN_S = 8.0
_NAV_TRIGGERS = frozenset({c.NAV_ACTIVE, c.NAV_DEST_LAT, c.NAV_DEST_LON, c.NAV_STOPS, c.NAV_RESULTS})


# --- geometry -----------------------------------------------------------------
def encode_polyline(points: list[LatLon], precision: int = 6) -> str:
    """Encoded polyline (Google algorithm); precision 6 is what Mapbox and OSRM use."""
    factor = 10 ** precision
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode_polyline(text: str, precision: int = 6) -> list[LatLon]:
    factor = 10 ** precision
    points: list[LatLon] = []
    index = lat = lon = 0
    while index < len(text):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(text[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / factor, lon / factor))
    return points


def _haversine_km(a: LatLon, b: LatLon) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(min(1.0, h)))


def bearing_deg(a: LatLon, b: LatLon) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def project_on_route(pts: list[LatLon], cum: list[float], here: LatLon,
                     near_km: float) -> tuple[float, float]:
    """(km along the route, metres off it) for the closest point to ``here``. Segments near the
    last known progress are tried first, so a route that doubles back is not mistaken."""
    lat0 = math.radians(here[0])

    def xy(p: LatLon) -> tuple[float, float]:
        return ((p[1] - here[1]) * 111.320 * math.cos(lat0), (p[0] - here[0]) * 110.574)

    def best(indices: range) -> tuple[float, float]:
        found = (near_km, float("inf"))
        for i in indices:
            (ax, ay), (bx, by) = xy(pts[i]), xy(pts[i + 1])
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 <= 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            off_km = math.hypot(ax + t * dx, ay + t * dy)
            if off_km * 1000.0 < found[1]:
                found = (cum[i] + t * (cum[i + 1] - cum[i]), off_km * 1000.0)
        return found

    window = range(len(pts) - 1)
    near = [i for i in window if cum[i + 1] >= near_km - 0.3 and cum[i] <= near_km + 3.0]
    km, off_m = best(range(near[0], near[-1] + 1)) if near else (near_km, float("inf"))
    return best(window) if off_m > OFF_ROUTE_M else (km, off_m)


def path_length_km(points: list[LatLon]) -> float:
    return sum(_haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1))


# --- routes -------------------------------------------------------------------
def safe_error(exc: BaseException) -> str:
    """An exception for the log, without its text: HTTP errors carry the URL and its token."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__} {status}" if status else type(exc).__name__


def mapbox_router(token: str, timeout_s: float = 4.0) -> Callable[[LatLon, LatLon], list[LatLon]]:
    """Mapbox Directions (driving) as a live router."""
    import httpx

    def route(a: LatLon, b: LatLon) -> list[LatLon]:
        url = (f"https://api.mapbox.com/directions/v5/mapbox/driving/"
               f"{a[1]},{a[0]};{b[1]},{b[0]}")
        r = httpx.get(url, params={"geometries": "polyline6", "overview": "full",
                                   "access_token": token}, timeout=timeout_s)
        r.raise_for_status()
        return decode_polyline(r.json()["routes"][0]["geometry"])

    return route


def osrm_router(timeout_s: float = 4.0,
                base: str = "https://router.project-osrm.org") -> Callable[[LatLon, LatLon], list[LatLon]]:
    """The public OSRM demo server as a no-key fallback router."""
    import httpx

    def route(a: LatLon, b: LatLon) -> list[LatLon]:
        url = f"{base}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
        r = httpx.get(url, params={"geometries": "polyline6", "overview": "full"},
                      timeout=timeout_s)
        r.raise_for_status()
        return decode_polyline(r.json()["routes"][0]["geometry"])

    return route


def live_router_from_env(service: str = "auto") -> tuple[Callable | None, str]:
    """(router, name) for config ``cabingym.route_service``: auto | mapbox | osrm | off."""
    service = (service or "auto").strip().lower()
    token = (os.environ.get("MAPBOX_API_KEY", "") or os.environ.get("MAPBOX_ACCESS_TOKEN", "")).strip()
    if service == "off":
        return None, ""
    if service in ("auto", "mapbox") and token:
        return mapbox_router(token), "mapbox"
    if service in ("auto", "osrm"):
        return osrm_router(), "osrm"
    return None, ""


class RouteBook:
    """Road geometry: baked routes between known places, then a live router, then a straight line."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        live: Callable[[LatLon, LatLon], list[LatLon]] | None = None,
        live_name: str = "osrm",
        tolerance_km: float = 0.3,
    ) -> None:
        data = json.loads(Path(path or ROUTES_FILE).read_text())
        self.places: dict[str, LatLon] = {
            name: (float(p["lat"]), float(p["lon"])) for name, p in data["places"].items()
        }
        self._routes: list[tuple[LatLon, LatLon, list[LatLon]]] = []
        for r in data["routes"].values():
            pts = [(float(lat), float(lon)) for lat, lon in r["points"]]
            self._routes.append((self.places[r["from"]], self.places[r["to"]], pts))
        self.live = live
        self.live_name = live_name
        self.tolerance_km = float(tolerance_km)
        self.last_geometry = ""

    def near(self, a: LatLon, b: LatLon) -> bool:
        return _haversine_km(a, b) <= self.tolerance_km

    def between(self, a: LatLon, b: LatLon) -> list[LatLon]:
        for start, end, pts in self._routes:
            if self.near(a, start) and self.near(b, end):
                self.last_geometry = "osm"
                return list(pts)
            if self.near(a, end) and self.near(b, start):
                self.last_geometry = "osm"
                return list(reversed(pts))
        if self.live is not None:
            try:
                pts = [(float(lat), float(lon)) for lat, lon in self.live(a, b)]
                if len(pts) >= 2:
                    self.last_geometry = self.live_name
                    return pts
            except Exception as exc:  # noqa: BLE001 — no network means a straight line, not a crash
                logger.info("route: live router failed (%s); straight line", safe_error(exc))
        self.last_geometry = "straight"
        return [a, b]


# --- the drive ----------------------------------------------------------------
def _has(store: Any, path: str) -> bool:
    try:
        store.signal(path)
        return True
    except Exception:  # noqa: BLE001
        return False


class SimDrive:
    """Moves the car along a route; parked (and silent) when it has none."""

    def __init__(
        self,
        cabin: CabinEnv,
        *,
        cruise_kph: float = 50.0,
        accel_mps2: float = 2.0,
        brake_mps2: float = 3.0,
        wh_per_km: float = 180.0,
        battery_kwh: float = 75.0,
    ) -> None:
        self.cabin = cabin
        self.store = cabin.store
        self.cruise_kph = float(cruise_kph)
        self.accel = float(accel_mps2)
        self.brake = float(brake_mps2)
        self._pct_per_km = wh_per_km / 1000.0 / battery_kwh * 100.0
        snap = self.store.snapshot()
        self._has_soc = _has(self.store, c.SOC)
        self._km_per_pct = (snap[c.RANGE] / snap[c.SOC]) if self._has_soc and snap[c.SOC] else 3.0
        self._rpm_path = c.resolve("rpm")
        self._has_rpm = _has(self.store, self._rpm_path)
        self._lock = threading.Lock()
        self._pts: list[LatLon] | None = None
        self._cum: list[float] = []
        self._pos_km = 0.0
        self._v = 0.0
        self._stopping = False
        self._nav_clock = 0.0
        self.off_route_s = 0.0
        self.on_arrive: Callable[[], None] | None = None
        # When set, remaining time comes from the route (seconds, km) instead of cruise speed.
        self.remaining_fn: Callable[[float], tuple[float, float]] | None = None

    @property
    def moving(self) -> bool:
        with self._lock:
            return self._pts is not None or self._v > 0.0

    def progress_km(self) -> float:
        """How far along the current route the car is (0 when parked)."""
        with self._lock:
            return self._pos_km if self._pts is not None else 0.0

    def follow(self, points: list[LatLon]) -> None:
        if len(points) < 2:
            return
        cum = [0.0]
        for i in range(len(points) - 1):
            cum.append(cum[-1] + _haversine_km(points[i], points[i + 1]))
        with self._lock:
            self._pts, self._cum, self._pos_km = list(points), cum, 0.0
            self._stopping = False
            self.off_route_s = 0.0

    def stop(self) -> None:
        with self._lock:
            if self._pts is not None:
                self._stopping = True

    def _where(self) -> tuple[LatLon, float]:
        pts, cum, pos = self._pts, self._cum, self._pos_km
        k = next((k for k in range(1, len(cum)) if cum[k] >= pos), len(cum) - 1)
        i = max(0, min(len(cum) - 2, k - 1))
        seg = cum[i + 1] - cum[i]
        f = 0.0 if seg <= 0 else (pos - cum[i]) / seg
        a, b = pts[i], pts[i + 1]
        here = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        return here, bearing_deg(a, b)

    def _real_fix(self) -> tuple[LatLon, float] | None:
        """(position, km/h) when a real receiver has a fix; then it moves the car, not the sim."""
        snap = self.store.snapshot(with_source=True)
        if not snap[c.GPS_FIX][0] or snap[c.LAT][1] not in REAL_SOURCES:
            return None
        return (float(snap[c.LAT][0]), float(snap[c.LON][0])), float(snap[c.SPEED][0] or 0.0)

    def _nav_writes(self, writes: dict[str, Any], dt_s: float, arrived: bool, total: float) -> None:
        self._nav_clock += dt_s
        if self.store.get(c.NAV_ACTIVE) and (self._nav_clock >= 1.0 or arrived):
            self._nav_clock = 0.0
            fn = self.remaining_fn
            if fn is not None:
                secs, km = fn(self._pos_km)
                writes[c.NAV_DIST] = round(max(0.0, km), 2)
                writes[c.NAV_ETA] = round(max(0.0, secs) / 60.0, 1)
            else:
                left_km = max(0.0, total - self._pos_km)
                writes[c.NAV_DIST] = round(left_km, 2)
                writes[c.NAV_ETA] = round(left_km / max(self.cruise_kph, 1.0) * 60.0, 1)

    def _energy_writes(self, writes: dict[str, Any], d_km: float) -> None:
        snap = self.store.snapshot()
        writes[c.ODOMETER] = round(snap[c.ODOMETER] + d_km, 3)
        if self._has_soc:
            soc = max(0.0, snap[c.SOC] - d_km * self._pct_per_km)
            writes[c.SOC] = round(soc, 3)
            writes[c.RANGE] = round(soc * self._km_per_pct, 1)

    def _step_real(self, dt_s: float, here: LatLon, kph: float) -> tuple[dict[str, Any], bool]:
        """Progress is where the real car is on the route; position, speed and heading stay the receiver's."""
        total = self._cum[-1]
        pos, off_m = project_on_route(self._pts, self._cum, here, self._pos_km)
        d_km = max(0.0, pos - self._pos_km)
        self._pos_km, self._v = pos, kph / 3.6
        self.off_route_s = self.off_route_s + dt_s if off_m > OFF_ROUTE_M else 0.0
        arrived = not self._stopping and total - pos <= _STOP_REACHED_KM
        writes: dict[str, Any] = {}
        self._energy_writes(writes, d_km)
        self._nav_writes(writes, dt_s, arrived, total)
        if arrived or self._stopping:
            self._pts, self._cum, self._pos_km, self._stopping = None, [], 0.0, False
            self.off_route_s = 0.0
        return writes, arrived

    def step(self, dt_s: float) -> list[Any]:
        arrived = False
        real = self._real_fix()
        with self._lock:
            if self._pts is None:
                return []
            if real is not None:
                writes, arrived = self._step_real(dt_s, *real)
            else:
                writes, arrived = self._step_sim(dt_s)
        changes = self.store.update(writes, source="sim")
        if arrived and self.on_arrive is not None:
            self.on_arrive()
        return changes

    def _step_sim(self, dt_s: float) -> tuple[dict[str, Any], bool]:
        arrived = False
        total = self._cum[-1]
        remaining_m = max(0.0, (total - self._pos_km) * 1000.0)
        cruise = self.cruise_kph / 3.6
        if self._stopping or remaining_m <= self._v * self._v / (2 * self.brake) + self._v * dt_s:
            target = 0.0
        else:
            target = cruise
        if self._v < target:
            self._v = min(target, self._v + self.accel * dt_s)
        else:
            self._v = max(target, self._v - self.brake * dt_s)
        if not self._stopping and remaining_m > 0.5 and self._v < 1.0:
            self._v = 1.0  # crawl the last metres instead of stopping short
        d_km = self._v * dt_s / 1000.0
        self._pos_km = min(total, self._pos_km + d_km)
        here, heading = self._where()
        if not self._stopping and self._pos_km >= total - 1e-6:
            self._v, arrived = 0.0, True
        parked = (self._stopping and self._v == 0.0) or arrived
        writes: dict[str, Any] = {
            c.SPEED: round(self._v * 3.6, 1),
            c.GEAR: "P" if parked else "D",
            c.LAT: round(here[0], 6), c.LON: round(here[1], 6),
            c.HEADING: float(round(heading)),
        }
        self._energy_writes(writes, d_km)
        if self._has_rpm:
            writes[self._rpm_path] = 0.0 if parked else float(round(800 + self._v * 60))
        self._nav_writes(writes, dt_s, arrived, total)
        if parked:
            self._pts, self._cum, self._pos_km, self._stopping = None, [], 0.0, False
        return writes, arrived


class NavFeed:
    """Keeps the drive and the ``nova.nav`` event in step with the navigation signals."""

    def __init__(
        self,
        cabin: CabinEnv,
        drive: SimDrive,
        routes: RouteBook,
        *,
        publish: Callable[[dict[str, Any]], None],
        autoplay: tuple[str, str] | None = None,
        dwell_s: float = 8.0,
        planner: Callable[..., Any] | None = None,
        voice_guidance: bool = False,
    ) -> None:
        self.cabin = cabin
        self.store = cabin.store
        self.drive = drive
        self.routes = routes
        self.publish = publish
        self.autoplay = autoplay
        self.dwell_s = float(dwell_s)
        self.dirty = False
        self._key: tuple | None = None
        self._route: list[LatLon] = []
        self._geometry = ""
        self._dwell: float | None = None
        # The stops still ahead, and how far along the route each one ends.
        self._stops: list[dict[str, Any]] = []
        self._leg_ends_km: list[float] = []
        self.planner = planner
        self.voice_guidance = bool(voice_guidance)
        self._plan: Any = None
        self._progress_clock = 0.0
        self._last_progress: tuple | None = None
        self._said: set[int] = set()
        self._pending_prompt: tuple[str, float] | None = None
        self._prompt_lock = threading.Lock()
        drive.on_arrive = self._arrived

    def on_changes(self, changes: list[Any]) -> None:
        """Store listener: only marks work to do (it can run inside a voice turn)."""
        if any(ch.path in _NAV_TRIGGERS for ch in changes):
            self.dirty = True

    def start(self) -> None:
        if self.autoplay:
            origin, dest = self.autoplay
            lat, lon = self.routes.places[origin]
            self.store.update({c.LAT: lat, c.LON: lon}, source="sim")
            self._navigate_to(dest)

    def _navigate_to(self, name: str) -> None:
        lat, lon = self.routes.places[name]
        here = (self.store.get(c.LAT), self.store.get(c.LON))
        km = path_length_km(self.routes.between(here, (lat, lon)))
        self.store.set_targets({
            c.NAV_ACTIVE: True, c.NAV_DEST: name, c.NAV_DEST_LAT: lat, c.NAV_DEST_LON: lon,
            c.NAV_DIST: round(km, 2),
            c.NAV_ETA: round(km / max(self.drive.cruise_kph, 1.0) * 60.0, 1),
            c.NAV_STOPS: (),
        })

    def _arrived(self) -> None:
        self.store.set_targets({c.NAV_ACTIVE: False, c.NAV_ETA: 0.0, c.NAV_DIST: 0.0})
        if self.autoplay:
            self._dwell = self.dwell_s

    def _plan_route(self, here: LatLon, stops: list[LatLon], dest: LatLon) -> Any:
        planner = self.planner
        if planner is None:
            return None
        try:
            return planner([here, *stops, dest])
        except Exception as exc:  # noqa: BLE001 — the message can contain the token
            logger.warning("directions failed (%s)", safe_error(exc))
            return None

    def _apply_book(self, here: LatLon, stops: list[LatLon], dest: LatLon) -> None:
        pts: list[LatLon] = []
        geometries = []
        ends: list[float] = []
        for target in [*stops, dest]:
            leg = self.routes.between(pts[-1] if pts else here, target)
            geometries.append(self.routes.last_geometry)
            pts.extend(leg if not pts else leg[1:])
            ends.append(path_length_km(pts))
        self._plan = None
        self.drive.remaining_fn = None
        self._route = pts
        self._leg_ends_km = ends
        self._geometry = "straight" if "straight" in geometries else (geometries[0] if geometries else "")
        self.cabin.route_points = list(pts)
        self.drive.follow(pts)

    def _ends_along(self, route: list[LatLon], stops: list[LatLon]) -> list[float]:
        cum = [0.0]
        for i in range(len(route) - 1):
            cum.append(cum[-1] + _haversine_km(route[i], route[i + 1]))
        ends: list[float] = []
        for stop in stops:
            target = (float(stop[0]), float(stop[1]))
            best = min(range(len(route)), key=lambda i: _haversine_km(route[i], target))
            ends.append(cum[best])
        ends.append(cum[-1] if cum else 0.0)
        return ends

    def _apply_plan(self, plan: Any, stops: list[LatLon]) -> None:
        self._plan = plan
        self._route = list(plan.points)
        if len(plan.leg_ends_km) == len(stops) + 1:
            self._leg_ends_km = list(plan.leg_ends_km)
        else:
            self._leg_ends_km = self._ends_along(self._route, stops)
        self._geometry = plan.geometry
        # plan.remaining only reads the plan. SimDrive.step calls it under the drive lock.
        self.drive.remaining_fn = plan.remaining
        self.cabin.route_points = list(self._route)
        self.drive.follow(self._route)
        secs, km = plan.remaining(0.0)
        self.store.set_targets({
            c.NAV_ETA: round(secs / 60.0, 1),
            c.NAV_DIST: round(km, 2),
        })

    def _clear_route(self) -> None:
        self._key = None
        self._route = []
        self.cabin.route_points = []
        self._geometry = ""
        self._stops = []
        self._leg_ends_km = []
        self._plan = None
        self._pending_prompt = None
        self._said.clear()
        self._last_progress = None
        self._progress_clock = 0.0
        self.drive.remaining_fn = None

    def sync(self) -> None:
        """Apply the current navigation state; may block on a live router (run off the loop)."""
        self.dirty = False
        snap = self.store.snapshot()
        dest = (snap[c.NAV_DEST_LAT], snap[c.NAV_DEST_LON])
        if snap[c.NAV_ACTIVE] and any(dest):
            stops = tuple((float(s["lat"]), float(s["lon"])) for s in snap[c.NAV_STOPS] or ())
            key = (dest, stops)
            if key != self._key:
                here = (snap[c.LAT], snap[c.LON])
                plan = self._plan_route(here, list(stops), dest)
                self._said.clear()
                self._pending_prompt = None
                self._last_progress = None
                self._progress_clock = 0.0
                if plan is not None and len(getattr(plan, "points", ()) or ()) >= 2:
                    self._apply_plan(plan, list(stops))
                else:
                    self._apply_book(here, list(stops), dest)
                self._key = key
                self._stops = [dict(s) for s in snap[c.NAV_STOPS] or ()]
        elif self._key is not None:
            self._clear_route()
            self.drive.stop()
        self.publish(self.event())

    def tick(self, dt_s: float) -> None:
        self.drive.step(dt_s)
        if self.drive.off_route_s >= OFF_ROUTE_REPLAN_S and self._key is not None:
            self.drive.off_route_s = 0.0
            self._key = None   # the next sync re-plans from the real position
            self.dirty = True
        self._drop_reached_stops()
        self._note_progress(dt_s)
        self._note_guidance()
        if self._dwell is not None and not self.drive.moving:
            self._dwell -= dt_s
            if self._dwell <= 0 and self.autoplay:
                self._dwell = None
                origin, dest = self.autoplay
                here = (self.store.get(c.LAT), self.store.get(c.LON))
                back = origin if self.routes.near(here, self.routes.places[dest]) else dest
                self._navigate_to(back)

    def _drop_reached_stops(self) -> None:
        """A stop the car has reached leaves the route, so a later re-plan (a new stop,
        a detour) goes from here through the stops still ahead, never back to it."""
        if not self._stops:
            return
        pos = self.drive.progress_km()
        here = (self.store.get(c.LAT), self.store.get(c.LON))
        reached = 0
        for i, stop in enumerate(self._stops):
            end = self._leg_ends_km[i] if i < len(self._leg_ends_km) else None
            near = _haversine_km(here, (float(stop["lat"]), float(stop["lon"]))) <= _STOP_REACHED_KM
            if near or (end is not None and pos > 0 and pos >= end - _STOP_REACHED_KM):
                reached = i + 1
        if reached:
            left = self._stops[reached:]
            self._stops = left  # until the re-plan replaces it
            self.store.set_targets({c.NAV_STOPS: tuple(left)})

    def _progress_event(self) -> dict[str, Any]:
        plan = self._plan
        pos = self.drive.progress_km()
        secs, km = plan.remaining(pos)
        step = plan.next_step(pos) if plan.steps else 0
        start = float(plan.steps[step]["start_km"]) if plan.steps else 0.0
        leg = 0
        for i, end in enumerate(plan.leg_ends_km):
            if pos < end:
                leg = i
                break
        else:
            leg = max(0, len(plan.leg_ends_km) - 1)
        limit = plan.speed_limit_kph(pos)
        return {
            "type": "nova.nav.progress",
            "step": step,
            "to_maneuver_m": int(round(max(0.0, (start - pos) * 1000.0))),
            "remaining_s": int(round(secs)),
            "remaining_m": int(round(km * 1000.0)),
            "leg": leg,
            "stops_left": len(self._stops),
            "speed_limit_kph": None if limit is None else int(round(limit)),
        }

    def _note_progress(self, dt_s: float) -> None:
        """About once a second, and only while a plan is the active route."""
        if self._plan is None or not self.store.get(c.NAV_ACTIVE):
            return
        self._progress_clock += dt_s
        if self._progress_clock < 1.0:
            return
        self._progress_clock = 0.0
        event = self._progress_event()
        key = (
            event["step"], event["to_maneuver_m"], event["remaining_s"], event["remaining_m"],
            event["leg"], event["stops_left"], event["speed_limit_kph"],
        )
        if key == self._last_progress:
            return
        self._last_progress = key
        self.publish(event)

    def _note_guidance(self) -> None:
        """Queue the latest prompt the car has reached, and drop one it has passed."""
        if not self.voice_guidance or self._plan is None or not self.store.get(c.NAV_ACTIVE):
            return
        pos = self.drive.progress_km()
        due: tuple[str, float] | None = None
        with self._prompt_lock:
            pending = self._pending_prompt
            if pending is not None and pos >= pending[1]:
                self._pending_prompt = None
            for i, prompt in enumerate(self._plan.voice):
                if i in self._said:
                    continue
                until = float(prompt["until_km"])
                text = str(prompt.get("text") or "")
                if pos >= until or not text:
                    self._said.add(i)
                    continue
                if pos >= float(prompt["at_km"]):
                    self._said.add(i)
                    due = (text, until)
            if due is not None:
                self._pending_prompt = due

    def guidance_waiting(self) -> bool:
        with self._prompt_lock:
            return self._pending_prompt is not None

    def take_guidance(self) -> str | None:
        """The pending line, if the car has not already passed it. One caller speaks it."""
        pos = self.drive.progress_km()
        with self._prompt_lock:
            pending = self._pending_prompt
            if pending is None:
                return None
            text, until = pending
            self._pending_prompt = None
        if pos >= until:
            return None
        return text

    def event(self) -> dict[str, Any]:
        snap = self.store.snapshot()
        active = bool(snap[c.NAV_ACTIVE])
        name = snap[c.NAV_DEST]
        here = (snap[c.LAT], snap[c.LON])
        pois = []
        for n, p in enumerate(snap[c.NAV_RESULTS] or (), start=1):
            km = _haversine_km(here, (float(p["lat"]), float(p["lon"])))
            pois.append({"n": n, "name": p["name"], "lat": p["lat"], "lon": p["lon"],
                         "category": p.get("category"), "distance_m": round(km * 1000),
                         "eta_min": round(km / max(self.drive.cruise_kph, 1.0) * 60.0, 1)})
        event: dict[str, Any] = {
            "type": "nova.nav",
            "active": active,
            "destination": ({"name": name, "lat": snap[c.NAV_DEST_LAT], "lon": snap[c.NAV_DEST_LON]}
                            if name else None),
            "route": encode_polyline(self._route) if active and self._route else "",
            "geometry": self._geometry if active else "",
            "eta_min": snap[c.NAV_ETA],
            "distance_km": snap[c.NAV_DIST],
            "pois": pois,
        }
        if not active:
            return event
        event["stops"] = [
            {"name": s.get("name"), "lat": s.get("lat"), "lon": s.get("lon")}
            for s in self._stops
        ]
        plan = self._plan
        if plan is None:
            return event
        secs, km = plan.remaining(self.drive.progress_km())
        event["duration_s"] = int(round(secs))
        event["distance_m"] = int(round(km * 1000.0))
        event["arrival_epoch"] = round(time.time() + secs)
        if plan.congestion:
            event["congestion"] = plan.congestion
        event["steps"] = [
            {key: step[key] for key in (
                "type", "modifier", "instruction", "street",
                "distance_m", "duration_s", "lat", "lon",
            )}
            for step in plan.steps
        ]
        if not self._stops and plan.alternatives:
            event["alternatives"] = [
                {"route": alt["route"], "duration_s": alt["duration_s"], "distance_m": alt["distance_m"]}
                for alt in plan.alternatives[:2]
            ]
        return event


def build_nav_feed(
    cfg_get: Callable[..., Any],
    cabin: CabinEnv,
    *,
    publish: Callable[[dict[str, Any]], None],
) -> NavFeed | None:
    """The drive + nav feed from config ``cabingym.sim_drive``; None when it is off.

    ``sim_drive``: ``enabled`` (bool), ``cruise_kph``, ``autoplay`` ([from, to] place
    names from the route book or ``cabingym.places``, or empty), ``route_service``
    (auto | mapbox | osrm | off).
    """
    from nova_hailo.cabingym.runtime import parse_places

    conf = cfg_get("cabingym", "sim_drive", default=None) or {}
    if not conf.get("enabled"):
        return None
    live, live_name = live_router_from_env(str(conf.get("route_service") or "auto"))
    routes = RouteBook(live=live, live_name=live_name or "osrm")
    routes.places.update(parse_places(cfg_get("cabingym", "places", default=None)))
    drive = SimDrive(cabin, cruise_kph=float(conf.get("cruise_kph") or 50.0))
    auto = conf.get("autoplay") or None
    autoplay = (str(auto[0]), str(auto[1])) if auto and len(auto) == 2 else None
    token = (os.environ.get("MAPBOX_API_KEY", "") or os.environ.get("MAPBOX_ACCESS_TOKEN", "")).strip()
    planner = None
    if token:
        from nova_hailo.cabingym.directions import mapbox_planner

        planner = mapbox_planner(token)
    voice = cfg_get("cabingym", "voice_guidance", default=True)
    return NavFeed(
        cabin, drive, routes, publish=publish, autoplay=autoplay,
        planner=planner, voice_guidance=True if voice is None else bool(voice),
    )


async def drive_loop(
    feed: NavFeed,
    *,
    hz: float = 5.0,
    sleep: Callable[[float], Any] | None = None,
    to_thread: Callable[[Callable[[], None]], Any] | None = None,
) -> None:
    """Advance the drive ``hz`` times a second; route changes are applied off the loop."""
    import asyncio

    sleep = sleep or asyncio.sleep
    to_thread = to_thread or (lambda f: asyncio.to_thread(f))
    period = 1.0 / hz
    while True:
        if feed.dirty:
            try:
                await to_thread(feed.sync)
            except Exception as exc:  # noqa: BLE001 — a routing failure must not end the drive
                logger.warning("nav sync failed (%s)", safe_error(exc))
        feed.tick(period)
        await sleep(period)
