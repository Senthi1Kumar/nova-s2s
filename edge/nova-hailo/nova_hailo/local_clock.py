"""Host IST (or configured) wall clock for spoken time and prompt stamps.

Callers: nova_hailo.edge_harness.tool_broker (get_datetime), context_history.
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"


def timezone_name() -> str:
    raw = (os.environ.get("NOVA_HAILO_TZ") or DEFAULT_TZ).strip()
    return raw or DEFAULT_TZ


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


def _tz_label(tz: ZoneInfo) -> str:
    if tz.key in {"Asia/Kolkata", "Asia/Calcutta"}:
        return "IST"
    return tz.key


def format_clock_stamp(now: datetime | None = None, tz_name: str | None = None) -> str:
    """Prompt stamp: 'Thursday, 11 September 2026, 10:06 IST'."""
    tz = _zone(tz_name)
    dt = _aware(now, tz)
    return dt.strftime(f"%A, %d %B %Y, %H:%M {_tz_label(tz)}")


def speak_local_time(now: datetime | None = None, tz_name: str | None = None) -> str:
    """Spoken clock: 'It's 10:39 PM IST, Tuesday 15 September.'"""
    tz = _zone(tz_name)
    dt = _aware(now, tz)
    hour = dt.strftime("%I").lstrip("0") or "12"
    minute = dt.strftime("%M")
    ampm = dt.strftime("%p")
    day = dt.strftime("%A")
    month = dt.strftime("%B")
    return f"It's {hour}:{minute} {ampm} {_tz_label(tz)}, {day} {dt.day} {month}."
