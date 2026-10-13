"""Wall clock for spoken time and prompt stamps, in the car's time zone.

Callers: nova_hailo.edge_harness.tool_broker (get_datetime), context_history,
pipeline (registers the cabin's zone and the configured fallback).

Zone precedence: NOVA_HAILO_TZ (operator override), then the zone of the car's
position (the GPS fix, else the simulated drive), then pipeline.timezone, then
Pacific time, where the simulated drive starts.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Los_Angeles"

_zone_source: Callable[[], str | None] | None = None
_fallback_tz: str | None = None

_SPOKEN = {
    "America/Los_Angeles": "Pacific time",
    "America/Denver": "Mountain time",
    "America/Phoenix": "Mountain time",
    "America/Chicago": "Central time",
    "America/New_York": "Eastern time",
    "Asia/Kolkata": "IST",
    "Asia/Calcutta": "IST",
}


def set_zone_source(source: Callable[[], str | None] | None, fallback: str | None = None) -> None:
    """Follow ``source()`` (an IANA name, or None when unknown); ``fallback`` when it has none."""
    global _zone_source, _fallback_tz
    _zone_source = source
    _fallback_tz = (fallback or "").strip() or None


def timezone_name() -> str:
    override = (os.environ.get("NOVA_HAILO_TZ") or "").strip()
    if override:
        return override
    if _zone_source is not None:
        try:
            zone = (_zone_source() or "").strip()
        except Exception:  # noqa: BLE001 -- a broken source must not break the clock
            zone = ""
        if zone:
            return zone
    return _fallback_tz or DEFAULT_TZ


def _zone(tz_name: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or timezone_name())
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _aware(now: datetime | None, tz: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _abbrev(tz: ZoneInfo, dt: datetime) -> str:
    """'PDT', 'IST'; the zone name when the abbreviation is only an offset."""
    name = dt.tzname() or ""
    return name if name.isalpha() else tz.key


def format_clock_stamp(now: datetime | None = None, tz_name: str | None = None) -> str:
    """Prompt stamp: 'Tuesday, 13 October 2026, 05:59 PDT'."""
    tz = _zone(tz_name)
    dt = _aware(now, tz)
    return dt.strftime(f"%A, %d %B %Y, %H:%M {_abbrev(tz, dt)}")


def speak_local_time(now: datetime | None = None, tz_name: str | None = None) -> str:
    """Spoken clock: 'It's 5:59 AM Pacific time, Tuesday 13 October.'"""
    tz = _zone(tz_name)
    dt = _aware(now, tz)
    hour = dt.strftime("%I").lstrip("0") or "12"
    minute = dt.strftime("%M")
    ampm = dt.strftime("%p")
    day = dt.strftime("%A")
    month = dt.strftime("%B")
    label = _SPOKEN.get(tz.key) or _abbrev(tz, dt)
    return f"It's {hour}:{minute} {ampm} {label}, {day} {dt.day} {month}."
