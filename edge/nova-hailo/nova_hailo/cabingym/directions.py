"""One Mapbox Directions v5 plan: duration, legs, steps, voice prompts, traffic.

Callers: NavFeed (the line and the countdown) and MapboxNav.route (the spoken ETA).
A failed call logs the exception class only. The message and the URL carry the token.
"""
from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nova_hailo.cabingym.drive import decode_polyline, safe_error

logger = logging.getLogger(__name__)

LatLon = tuple[float, float]
_EARTH_KM = 6371.0
_ORIGIN_CACHE_KM = 0.06  # a car that crept ~45 m reuses the last plan
_STEP_CAP = 80
_CONGESTION = {"low": "l", "moderate": "m", "heavy": "h", "severe": "s", "unknown": "u"}
_BASE = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"

_cache: list[tuple[LatLon, tuple[LatLon, ...], tuple[str, ...], RoutePlan]] = []
_cache_lock = threading.Lock()


def _haversine_km(a: LatLon, b: LatLon) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(min(1.0, h)))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


@dataclass
class RoutePlan:
    geometry: str
    duration_s: float
    distance_m: float
    points: list[LatLon]
    leg_ends_km: list[float]
    congestion: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    voice: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    _leg_dur_s: list[float] = field(default_factory=list)
    _limits: list[tuple[float, float | None]] = field(default_factory=list)

    def remaining(self, progress_km: float) -> tuple[float, float]:
        """Seconds and kilometres still ahead, from the route's own legs."""
        ends = self.leg_ends_km
        if not ends or progress_km >= ends[-1]:
            return (0.0, 0.0)
        start = 0.0
        for i, end in enumerate(ends):
            if progress_km < end or i == len(ends) - 1:
                span = max(end - start, 1e-9)
                left = max(0.0, end - progress_km) / span
                secs = self._leg_dur_s[i] * left
                km = (end - start) * left
                for j in range(i + 1, len(ends)):
                    secs += self._leg_dur_s[j]
                    km += ends[j] - ends[j - 1]
                return (secs, km)
            start = end
        return (0.0, 0.0)

    def speed_limit_kph(self, progress_km: float) -> float | None:
        for end, kph in self._limits:
            if progress_km <= end:
                return kph
        return self._limits[-1][1] if self._limits else None

    def next_step(self, progress_km: float) -> int:
        """Index of the next maneuver. ``steps`` is empty on a straight plan."""
        for i, step in enumerate(self.steps):
            if float(step["start_km"]) > progress_km:
                return i
        return max(0, len(self.steps) - 1)


def _letter(raw: Any) -> str:
    if raw is None:
        return "u"
    return _CONGESTION.get(str(raw).lower(), "u")


def _limit_kph(raw: Any) -> float | None:
    if not isinstance(raw, dict) or raw.get("unknown") or raw.get("speed") is None:
        return None
    speed = float(raw["speed"])
    unit = str(raw.get("unit") or "km/h").lower()
    if unit in {"mph", "mi/h"}:
        speed *= 1.60934
    return speed


def plan_from_mapbox(body: dict[str, Any]) -> RoutePlan:
    route = (body.get("routes") or [{}])[0]
    legs = list(route.get("legs") or [])
    points = decode_polyline(str(route.get("geometry") or ""))
    ends: list[float] = []
    durations: list[float] = []
    cursor_m = 0.0
    steps: list[dict[str, Any]] = []
    voice: list[dict[str, Any]] = []
    letters: list[str] = []
    limits: list[tuple[float, float | None]] = []
    for leg_i, leg in enumerate(legs):
        leg_m = float(leg.get("distance") or 0.0)
        cursor_m += leg_m
        ends.append(cursor_m / 1000.0)
        durations.append(float(leg.get("duration") or 0.0))
        ann = leg.get("annotation") or {}
        dists = [float(d) for d in (ann.get("distance") or [])]
        raw_cong = ann.get("congestion")
        raw_speed = ann.get("maxspeed") or []
        nseg = len(dists) if dists else (len(raw_cong) if raw_cong else 0)
        seg_cursor = ends[-1] - leg_m / 1000.0
        walked = 0.0
        for i in range(nseg):
            dist_m = dists[i] if i < len(dists) else 0.0
            walked += dist_m
            seg_cursor += dist_m / 1000.0
            raw = raw_cong[i] if raw_cong and i < len(raw_cong) else None
            letters.append(_letter(raw))
            limits.append((seg_cursor, _limit_kph(raw_speed[i] if i < len(raw_speed) else None)))
        if nseg and abs(walked - leg_m) > 1.0 and not dists:
            limits.append((ends[-1], None))
        step_m = ends[-1] * 1000.0 - leg_m
        for step in leg.get("steps") or []:
            if len(steps) >= _STEP_CAP:
                break
            man = step.get("maneuver") or {}
            loc = man.get("location") or [0.0, 0.0]
            dist_m = float(step.get("distance") or 0.0)
            steps.append({
                "type": str(man.get("type") or ""),
                "modifier": str(man.get("modifier") or ""),
                "instruction": str(man.get("instruction") or ""),
                "street": str(step.get("name") or ""),
                "distance_m": dist_m,
                "duration_s": float(step.get("duration") or 0.0),
                "lat": float(loc[1]),
                "lon": float(loc[0]),
                "leg": leg_i,
                "start_km": step_m / 1000.0,
            })
            end_m = step_m + dist_m
            for prompt in step.get("voiceInstructions") or []:
                along = float(prompt.get("distanceAlongGeometry") or 0.0)
                voice.append({
                    "at_km": (end_m - along) / 1000.0,
                    "until_km": end_m / 1000.0,
                    "text": str(prompt.get("announcement") or ""),
                })
            step_m = end_m
    alternatives = [
        {
            "route": str(alt.get("geometry") or ""),
            "duration_s": float(alt.get("duration") or 0.0),
            "distance_m": float(alt.get("distance") or 0.0),
        }
        for alt in (body.get("routes") or [])[1:]
    ]
    return RoutePlan(
        geometry="mapbox",
        duration_s=float(route.get("duration") or 0.0),
        distance_m=float(route.get("distance") or 0.0),
        points=points,
        leg_ends_km=ends,
        congestion="".join(letters),
        steps=steps,
        voice=voice,
        alternatives=alternatives,
        _leg_dur_s=durations,
        _limits=limits,
    )


def straight_plan(points: list[LatLon], legs: int = 1, cruise_kph: float = 40.0) -> RoutePlan:
    pts = [(float(p[0]), float(p[1])) for p in points]
    cum = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + _haversine_km(pts[i], pts[i + 1]))
    total = cum[-1]
    nlegs = max(1, int(legs))
    if len(pts) - 1 == nlegs:
        ends = cum[1:]
    elif total <= 0:
        ends = [0.0] * nlegs
    else:
        ends = [total * (i + 1) / nlegs for i in range(nlegs)]
    distance_m = total * 1000.0
    duration_s = distance_m / 1000.0 / max(float(cruise_kph), 1.0) * 3600.0
    durations = []
    prev = 0.0
    for end in ends:
        span = max(end - prev, 0.0)
        durations.append(duration_s * (span / total) if total > 0 else 0.0)
        prev = end
    return RoutePlan(
        geometry="straight",
        duration_s=duration_s,
        distance_m=distance_m,
        points=pts,
        leg_ends_km=ends,
        congestion="",
        _leg_dur_s=durations,
    )


def _default_fetch(timeout_s: float) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    import httpx

    def fetch(url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = httpx.get(url, params=params, timeout=timeout_s)
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {}

    return fetch


def mapbox_planner(
    token: str,
    fetch: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    timeout_s: float = 4.0,
) -> Callable[..., RoutePlan | None]:
    """A planner(waypoints, exclude=()) that makes one Directions call and caches it."""
    get = fetch or _default_fetch(timeout_s)

    def plan(waypoints: list[LatLon], exclude: tuple[str, ...] = ()) -> RoutePlan | None:
        if len(waypoints) < 2:
            return None
        origin = (float(waypoints[0][0]), float(waypoints[0][1]))
        rest = tuple((float(p[0]), float(p[1])) for p in waypoints[1:])
        dropped = tuple(str(item) for item in exclude)
        with _cache_lock:
            for cached_origin, cached_rest, cached_exclude, cached in _cache:
                if cached_exclude != dropped or cached_rest != rest:
                    continue
                if _haversine_km(cached_origin, origin) <= _ORIGIN_CACHE_KM:
                    return cached
        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in (origin, *rest))
        params: dict[str, Any] = {
            "access_token": token,
            "geometries": "polyline6",
            "overview": "full",
            "steps": "true",
            "banner_instructions": "true",
            "voice_instructions": "true",
            "voice_units": "metric",
            "annotations": "distance,duration,maxspeed,congestion",
            "alternatives": "false" if len(waypoints) > 2 else "true",
        }
        if dropped:
            params["exclude"] = ",".join(dropped)
        try:
            body = get(_BASE + coords, params)
        except Exception as exc:  # noqa: BLE001 — the message can contain the token
            logger.warning("directions failed (%s)", safe_error(exc))
            return None
        if not isinstance(body, dict) or body.get("code") != "Ok" or not body.get("routes"):
            return None
        built = plan_from_mapbox(body)
        with _cache_lock:
            _cache.append((origin, rest, dropped, built))
        return built

    return plan
