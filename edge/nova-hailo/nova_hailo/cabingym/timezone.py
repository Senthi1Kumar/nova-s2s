"""IANA time zone for the car's position, so the clock follows the car.

Callers: nova_hailo.cabingym.catalog (build_store keeps Location.TimeZone in
step with the position), tests.

The position is the real GPS fix when there is one, else the simulated drive
(Palo Alto by default), so indoors the clock shows the simulated location's
time. ``tzfpy`` gives exact borders when it is installed; without it a coarse
table covers the regions the demo drives in, and anywhere else falls back to
the nautical zone for the longitude (``Etc/GMT+8`` and so on, no DST).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

# (south, west, north, east, zone); first match wins, so narrower boxes come first.
_BOXES: tuple[tuple[float, float, float, float, str], ...] = (
    (31.3, -114.8, 37.0, -109.0, "America/Phoenix"),
    (32.0, -125.0, 49.0, -114.0, "America/Los_Angeles"),
    (31.0, -114.0, 49.0, -102.0, "America/Denver"),
    (25.0, -102.0, 49.0, -87.0, "America/Chicago"),
    (24.0, -87.0, 47.5, -66.5, "America/New_York"),
    (6.0, 68.0, 36.0, 97.5, "Asia/Kolkata"),
    (49.8, -8.2, 60.9, 1.8, "Europe/London"),
    (36.0, -9.5, 55.0, 15.0, "Europe/Berlin"),
    (24.0, 123.0, 46.0, 146.0, "Asia/Tokyo"),
)


def _exact() -> Callable[[float, float], str] | None:
    try:
        from tzfpy import get_tz  # type: ignore[import-not-found]
    except ImportError:
        return None
    return lambda lat, lon: str(get_tz(lon, lat) or "")


_EXACT = _exact()


def zone_for(lat: float, lon: float) -> str:
    if _EXACT is not None:
        zone = _EXACT(lat, lon)
        if zone:
            return zone
    for south, west, north, east, zone in _BOXES:
        if south <= lat <= north and west <= lon <= east:
            return zone
    hours = round(lon / 15.0)
    # Etc/GMT signs are inverted: Etc/GMT+8 is UTC-8.
    return "UTC" if hours == 0 else f"Etc/GMT{-hours:+d}"


def track_timezone(store: Any, lat_path: str, lon_path: str, tz_path: str) -> Callable[[], None]:
    """Keep ``tz_path`` equal to the zone of the store's position; returns the unsubscribe."""

    def refresh() -> None:
        zone = zone_for(float(store.get(lat_path)), float(store.get(lon_path)))
        if zone != store.get(tz_path):
            # Derived, never owned by a real source, so it is always writable as sim.
            store.update({tz_path: zone}, source="sim")

    def on_changes(changes: list[Any]) -> None:
        if any(ch.path in (lat_path, lon_path) for ch in changes):
            refresh()

    refresh()
    return store.subscribe(on_changes)
