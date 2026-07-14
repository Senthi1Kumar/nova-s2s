"""Personal Google Calendar via OAuth + Calendar JSON API.

Caller: nova/server/tool_service.py build_registry (replaces CalendarStubTool).

Workspace remote MCP ``tools/call`` still returns permission denied even with
``roles/mcp.toolUser`` (Developer Preview). The same OAuth token works against
``calendar.googleapis.com`` REST, so reads/writes go through REST. MCP client
stays available for later when Google opens tool calls.

Create/delete need ``https://www.googleapis.com/auth/calendar.events`` —
re-auth after upgrading from readonly scopes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import re

import httpx

from nova.tools.base import NovaTool
from nova.tools.mcp.oauth import GoogleTokenProvider

_UNSET: Any = object()
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _oauth_access_or_error(tokens: GoogleTokenProvider | None) -> str | dict[str, Any]:
    if tokens is None or not tokens.configured():
        return {
            "status": "unavailable",
            "reason": "google_oauth_not_configured",
            "hint": "Set GOOGLE_OAUTH_CLIENT_ID/SECRET in .env, then connect Google in Settings.",
        }
    if not tokens.authenticated():
        return {
            "status": "unavailable",
            "reason": "google_oauth_not_authenticated",
            "hint": "Open Settings → Connect with Google (work Chrome profile).",
        }
    access = tokens.get_access_token()
    if not access:
        return {
            "status": "unavailable",
            "reason": "google_oauth_token_refresh_failed",
            "hint": "Reconnect Google Calendar in Settings.",
        }
    return access


class CheckCalendarTool(NovaTool):
    """Upcoming personal calendar events (Google Calendar, OAuth)."""

    name = "check_calendar"
    description = (
        "Check the user's personal Google Calendar for upcoming events "
        "(meetings, schedule). Prefer this over query_calendar for personal/work meetings. "
        "Pass day=today|tomorrow|day_after_tomorrow, or on_date=YYYY-MM-DD for a specific date; "
        "otherwise day=week. Speak the returned speak field verbatim — do not invent dates."
    )
    parameters = {
        "type": "object",
        "properties": {
            "day": {
                "type": "string",
                "enum": ["today", "tomorrow", "day_after_tomorrow", "week"],
                "description": "Relative day window. Default week.",
            },
            "on_date": {
                "type": "string",
                "description": "Optional calendar day YYYY-MM-DD (e.g. 2026-07-16). Overrides day.",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        tokens: GoogleTokenProvider | None = _UNSET,
        timeout: float = 20.0,
        http_get=None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._http_get = http_get or httpx.get

    def execute(self, day: str = "week", on_date: str = "") -> dict[str, Any]:
        access = _oauth_access_or_error(self._tokens)
        if isinstance(access, dict):
            return access

        day_key = (day or "week").strip().lower()
        if day_key not in {"today", "tomorrow", "day_after_tomorrow", "week"}:
            day_key = "week"
        on_date_s = (on_date or "").strip()
        if on_date_s and len(on_date_s) >= 10:
            on_date_s = on_date_s[:10]

        now = datetime.now(timezone.utc)
        # Fetch far enough to cover "day after tomorrow" and named dates this week.
        later = now + timedelta(days=10)
        try:
            resp = self._http_get(
                CALENDAR_EVENTS_URL,
                params={
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 25,
                    "timeMin": now.isoformat().replace("+00:00", "Z"),
                    "timeMax": later.isoformat().replace("+00:00", "Z"),
                },
                headers={"Authorization": f"Bearer {access}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — voice loop must not crash
            return {
                "status": "unavailable",
                "reason": "calendar_api_error",
                "error": str(exc)[:300],
            }

        events = _normalize_events(payload)
        events = _filter_events_by_day(events, day_key, now, on_date=on_date_s)
        label = on_date_s or day_key
        speak = _speak_events(events, label)
        return {
            "status": "success",
            "day": day_key if not on_date_s else "on_date",
            "on_date": on_date_s or None,
            "event_count": len(events),
            "events": events[:10],
            "speak": speak,
            "source": "google_calendar_api",
            "window": {"from": now.isoformat(), "to": later.isoformat()},
        }


class CreateCalendarEventTool(NovaTool):
    """Create an event on the primary Google Calendar (irreversible — ConfirmationGate)."""

    name = "create_calendar_event"
    description = (
        "Create a meeting or event on the user's personal Google Calendar. "
        "Irreversible: confirm title and start time with the driver before calling "
        "with confirmed=true. Use ISO-8601 datetimes with timezone offset "
        "(e.g. 2026-07-15T15:00:00+05:30)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title / meeting name."},
            "start": {
                "type": "string",
                "description": "Start datetime ISO-8601 with offset, e.g. 2026-07-15T15:00:00+05:30.",
            },
            "end": {
                "type": "string",
                "description": "Optional end datetime ISO-8601. Defaults to start + 1 hour.",
            },
            "description": {
                "type": "string",
                "description": "Optional event description / notes.",
            },
        },
        "required": ["title", "start"],
    }

    def __init__(
        self,
        tokens: GoogleTokenProvider | None = _UNSET,
        timeout: float = 20.0,
        http_post: Callable[..., Any] | None = None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._http_post = http_post or httpx.post

    def execute(
        self,
        title: str,
        start: str,
        end: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        access = _oauth_access_or_error(self._tokens)
        if isinstance(access, dict):
            return access

        title = (title or "").strip()
        start = (start or "").strip()
        if not title or not start:
            return {
                "status": "error",
                "reason": "missing_fields",
                "hint": "Need title and start (ISO-8601 with timezone).",
            }

        end_s = (end or "").strip() or _default_end(start)
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start},
            "end": {"dateTime": end_s},
        }
        if description and description.strip():
            body["description"] = description.strip()

        try:
            resp = self._http_post(
                CALENDAR_EVENTS_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": "calendar_api_error",
                "error": str(exc)[:300],
                "hint": (
                    "If this is a 403 insufficient scopes, Disconnect then Connect "
                    "Google again so write scope calendar.events is granted."
                ),
            }

        return {
            "status": "success",
            "action": "created",
            "event": {
                "id": str(payload.get("id") or ""),
                "title": str(payload.get("summary") or title),
                "start": start,
                "end": end_s,
            },
            "source": "google_calendar_api",
        }


class DeleteCalendarEventTool(NovaTool):
    """Delete an event from the primary Google Calendar (irreversible — ConfirmationGate)."""

    name = "delete_calendar_event"
    description = (
        "Delete a meeting or event from the user's personal Google Calendar. "
        "Pass event_id from check_calendar when known, or title to match an upcoming "
        "event. Irreversible: confirm which event with the driver, then call with "
        "confirmed=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Google Calendar event id from check_calendar.",
            },
            "title": {
                "type": "string",
                "description": "Match an upcoming event by title substring if event_id unknown.",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        tokens: GoogleTokenProvider | None = _UNSET,
        timeout: float = 20.0,
        http_get=None,
        http_delete: Callable[..., Any] | None = None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._http_get = http_get or httpx.get
        self._http_delete = http_delete or httpx.delete

    def execute(self, event_id: str = "", title: str = "") -> dict[str, Any]:
        access = _oauth_access_or_error(self._tokens)
        if isinstance(access, dict):
            return access

        event_id = (event_id or "").strip()
        title = (title or "").strip()
        matched_title = title

        if not event_id:
            if not title:
                return {
                    "status": "error",
                    "reason": "missing_event",
                    "hint": "Pass event_id from check_calendar, or title to match.",
                }
            found = self._find_by_title(access, title)
            if found is None:
                return {
                    "status": "error",
                    "reason": "event_not_found",
                    "hint": f"No upcoming event matching title '{title}'.",
                }
            if isinstance(found, dict) and found.get("status") == "unavailable":
                return found
            event_id = found["id"]
            matched_title = found["title"]

        url = f"{CALENDAR_EVENTS_URL}/{event_id}"
        try:
            resp = self._http_delete(
                url,
                headers={"Authorization": f"Bearer {access}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": "calendar_api_error",
                "error": str(exc)[:300],
                "hint": (
                    "If this is a 403 insufficient scopes, Disconnect then Connect "
                    "Google again so write scope calendar.events is granted."
                ),
            }

        return {
            "status": "success",
            "action": "deleted",
            "event": {"id": event_id, "title": matched_title},
            "source": "google_calendar_api",
        }

    def _find_by_title(self, access: str, title: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        later = now + timedelta(days=14)
        try:
            resp = self._http_get(
                CALENDAR_EVENTS_URL,
                params={
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 25,
                    "timeMin": now.isoformat().replace("+00:00", "Z"),
                    "timeMax": later.isoformat().replace("+00:00", "Z"),
                },
                headers={"Authorization": f"Bearer {access}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": "calendar_api_error",
                "error": str(exc)[:300],
            }

        needle = title.casefold()
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "")
            if needle in summary.casefold():
                return {"id": str(item.get("id") or ""), "title": summary or title}
        return None


def _default_end(start: str) -> str:
    """Parse start ISO and return start+1h; on parse failure return start unchanged."""
    try:
        s = start.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return (dt + timedelta(hours=1)).isoformat()
    except ValueError:
        return start


def _parse_event_start(start_s: str) -> datetime | None:
    if not start_s:
        return None
    try:
        # All-day dates are YYYY-MM-DD
        if len(start_s) == 10 and start_s[4] == "-":
            return datetime.fromisoformat(start_s).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(start_s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _filter_events_by_day(
    events: list[dict[str, str]],
    day_key: str,
    now_utc: datetime,
    on_date: str = "",
) -> list[dict[str, str]]:
    local_now = now_utc.astimezone()
    if on_date:
        try:
            target = datetime.fromisoformat(on_date).date()
        except ValueError:
            return []
    elif day_key == "week":
        return events
    else:
        target = local_now.date()
        if day_key == "tomorrow":
            target = target + timedelta(days=1)
        elif day_key == "day_after_tomorrow":
            target = target + timedelta(days=2)
    out: list[dict[str, str]] = []
    for ev in events:
        dt = _parse_event_start(ev.get("start") or "")
        if dt is None:
            continue
        if dt.astimezone().date() == target:
            out.append(ev)
    return out


_HOUR_WORDS = {
    0: "twelve", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
    12: "twelve",
}


def _speak_clock(dt: datetime) -> str:
    """Phonetically friendly clock for TTS (e.g. 'two thirty PM')."""
    local = dt.astimezone()
    h24 = local.hour
    minute = local.minute
    ampm = "AM" if h24 < 12 else "PM"
    h12 = h24 % 12
    hour_w = _HOUR_WORDS[h12]
    if minute == 0:
        return f"{hour_w} {ampm}"
    if minute == 15:
        return f"{hour_w} fifteen {ampm}"
    if minute == 30:
        return f"{hour_w} thirty {ampm}"
    if minute == 45:
        return f"{hour_w} forty five {ampm}"
    return f"{hour_w} {minute} {ampm}"


def _speak_events(events: list[dict[str, str]], day_key: str) -> str:
    """Ready-to-speak summary — model must read this aloud, not invent dates."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_key or ""):
        label = day_key
        empty = f"You have no events on {day_key}."
        week_mode = False
    else:
        label = {
            "today": "today",
            "tomorrow": "tomorrow",
            "day_after_tomorrow": "the day after tomorrow",
            "week": "coming up",
        }.get(day_key, "coming up")
        empty = {
            "tomorrow": "You have no events tomorrow.",
            "today": "You have no events left today.",
            "day_after_tomorrow": "You have no events the day after tomorrow.",
        }.get(day_key, "You have no upcoming events this week.")
        week_mode = day_key == "week"

    if not events:
        return empty

    parts: list[str] = []
    for ev in events[:6]:
        title = ev.get("title") or "untitled"
        dt = _parse_event_start(ev.get("start") or "")
        if dt is None:
            parts.append(title)
            continue
        when = _speak_clock(dt)
        if week_mode:
            day_name = dt.astimezone().strftime("%A")
            parts.append(f"{title} on {day_name} at {when}")
        else:
            parts.append(f"{title} at {when}")

    joined = "; ".join(parts)
    n = len(events)
    more = f" And {n - 6} more." if n > 6 else ""
    if week_mode:
        return f"You have {n} upcoming events. {joined}.{more}".strip()
    return f"You have {n} events {label}. {joined}.{more}".strip()


def _normalize_events(result: Any) -> list[dict[str, str]]:
    """Flatten Calendar API / MCP-shaped result into speakable {id, title, start} rows."""
    raw_items: list[Any] = []
    if isinstance(result, dict):
        structured = result.get("structuredContent") or result.get("structured_content")
        if isinstance(structured, dict):
            raw_items = structured.get("events") or structured.get("items") or []
        elif isinstance(structured, list):
            raw_items = structured
        raw_items = raw_items or result.get("events") or result.get("items") or []
        if not raw_items and "title" in result:
            raw_items = [result]
    elif isinstance(result, list):
        raw_items = result

    out: list[dict[str, str]] = []
    for item in raw_items[:10]:
        if not isinstance(item, dict):
            continue
        title = str(
            item.get("summary")
            or item.get("title")
            or item.get("name")
            or "untitled"
        )
        start = item.get("start") or item.get("startTime") or item.get("start_time")
        if isinstance(start, dict):
            start_s = str(
                start.get("dateTime") or start.get("date") or start.get("date_time") or ""
            )
        else:
            start_s = str(item.get("when") or start or "")
        row = {"title": title, "start": start_s}
        eid = item.get("id") or item.get("event_id")
        if eid:
            row["id"] = str(eid)
        out.append(row)
    return out
