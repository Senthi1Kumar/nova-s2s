"""Simulated CAN/OBD vehicle state + reminder/calendar persistence, exposed as
atomic ``NovaTool``s.

Ported from ``../nova_ai/litert_lm_chat_app/app/vehicle_db.py`` (SQLite schema,
default state, and the HVAC/sunroof/windows interlock logic — proven, real
simulator logic). NOT ported: ``app/vehicle_tools.py``'s ``SchemaTool`` /
``build_vehicle_tools`` / ``intent_schema.build_toolbox`` machinery and its
generic ``action``-enum dispatch shape — that was purpose-built for a
supervised fine-tune's fixed toolbox and is the wrong external contract for a
base model calling atomic, individually-named tools (see CLAUDE.md and the
Task 3 brief). ``VehicleDB`` below keeps the useful internal logic
(interlock, host-clock reminder resolution) but is driven by atomic methods
rather than free-form ``args: dict`` dispatch.

SQLite (stdlib) — NOT a vector DB: everything here is structured data with
exact lookups. One file, opened per call (cheap, thread-safe). The DB path is
a constructor argument (not a hardcoded module-level path) so tests can point
it at a temp file for isolation.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from nova.tools.base import NovaTool

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "runtime" / "vehicle.db"

ZONES = ("driver", "passenger", "rear")

_DEFAULT_STATE: dict[str, Any] = {
    "cabin_temp_c": 21.0,
    "hvac_mode": "auto",
    "fan_level": 2,
    "hvac_zones": {z: "off" for z in ZONES},
    "zone_temp_c": {z: 21.0 for z in ZONES},
    "sunroof": "closed",
    "windows": "closed",
    "defrost": "off",
    "lights": "auto",
    "media": "off",
    "media_content": "",
    "volume": 40,
    "phone_connected": True,
    # OBD-ish read-only signals (query_vehicle_status targets)
    "fuel_level_pct": 68,
    "range_km": 412,
    "battery_soc_pct": 81,
    "odometer_km": 24310,
    "engine_status": "ok",
    "coolant_temp_c": 89,
    "tire_pressure_psi": {"fl": 34, "fr": 34, "rl": 33, "rr": 34},
    "dtc_codes": [],
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def resolve_when(*phrases: str) -> str:
    """Resolve loose natural-language time hints against the HOST CLOCK to an
    absolute ISO timestamp. Handles today/tomorrow/tonight and times like
    '4pm', '16:00', '11:30am'. Returns '' when nothing parses."""
    text = " ".join(p.lower() for p in phrases)
    day = dt.date.today()
    if "tomorrow" in text:
        day += dt.timedelta(days=1)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if not m and "tomorrow" not in text and "today" not in text and "tonight" not in text:
        return ""
    hh, mm = (int(m.group(1)), int(m.group(2) or 0)) if m else (9, 0)
    if m and m.group(3) == "pm" and hh < 12:
        hh += 12
    if "tonight" in text and hh < 12:
        hh += 12
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    when = dt.datetime.combine(day, dt.time(hh, mm))
    if when < dt.datetime.now() and "today" not in text:
        when += dt.timedelta(days=1)  # past time w/o a day -> next occurrence
    return when.strftime("%Y-%m-%dT%H:%M:%S")


class VehicleDB:
    """SQLite-backed simulated vehicle state + reminders/calendar.

    ``db_path`` is explicit (defaults to ``runtime/vehicle.db``) so tests can
    point at an isolated temp file instead of the real runtime DB.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db_path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS vehicle_state(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reminders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL, due_ts TEXT DEFAULT '',
                    done INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calendar_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, start_ts TEXT NOT NULL,
                    end_ts TEXT DEFAULT '', created_at TEXT NOT NULL);
                """
            )
            now = _now()
            for k, v in _DEFAULT_STATE.items():
                c.execute(
                    "INSERT OR IGNORE INTO vehicle_state VALUES (?,?,?)",
                    (k, json.dumps(v), now),
                )
            if not c.execute("SELECT 1 FROM calendar_events LIMIT 1").fetchone():
                tmw = dt.date.today() + dt.timedelta(days=1)
                for title, hhmm in (("Team standup", "09:30"), ("Elevatex AI review", "16:00")):
                    c.execute(
                        "INSERT INTO calendar_events(title,start_ts,created_at) VALUES (?,?,?)",
                        (title, f"{tmw}T{hhmm}:00", now),
                    )

    def _set_state(self, c: sqlite3.Connection, key: str, value: Any) -> None:
        c.execute(
            "INSERT INTO vehicle_state VALUES (?,?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), _now()),
        )

    def get_state(self) -> dict[str, Any]:
        with self._conn() as c:
            return {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key,value FROM vehicle_state")}

    def get_reminders(self, include_done: bool = False) -> list[dict]:
        q = "SELECT * FROM reminders" + ("" if include_done else " WHERE done=0")
        with self._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT 20")]

    def get_events(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM calendar_events ORDER BY id LIMIT 20")]

    def _hvac_interlock(self, c: sqlite3.Connection, state: dict) -> list[str]:
        """Real-vehicle rule: whenever any HVAC zone is running, sunroof and
        windows must be (and stay) closed. Returns what was auto-closed."""
        closed = []
        if any(v == "on" for v in state["hvac_zones"].values()):
            for opening in ("sunroof", "windows"):
                if state.get(opening) not in ("closed", None):
                    self._set_state(c, opening, "closed")
                    closed.append(opening)
        return closed

    # ---------- atomic operations backing the NovaTools below ----------

    def set_hvac(self, zone: str, on: bool, target_temp_c: float | None = None) -> dict:
        zones = list(ZONES) if zone == "all" else [zone]
        with self._conn() as c:
            state = self.get_state()
            hz, zt = state["hvac_zones"], state["zone_temp_c"]
            for z in zones:
                hz[z] = "on" if on else "off"
                if on and isinstance(target_temp_c, (int, float)) and 14 <= float(target_temp_c) <= 30:
                    zt[z] = float(target_temp_c)
            self._set_state(c, "hvac_zones", hz)
            self._set_state(c, "zone_temp_c", zt)
            self._set_state(c, "hvac_mode", "off" if all(v == "off" for v in hz.values()) else "auto")
            if on and zone == "all" and isinstance(target_temp_c, (int, float)) and 14 <= float(target_temp_c) <= 30:
                self._set_state(c, "cabin_temp_c", float(target_temp_c))
            state["hvac_zones"] = hz
            auto_closed = self._hvac_interlock(c, state)
        out: dict[str, Any] = {"status": "success", "zone": zone, "on": on}
        if target_temp_c is not None:
            out["target_temp_c"] = target_temp_c
        if auto_closed:
            out["auto_closed"] = auto_closed
            out["note"] = "sunroof/windows were closed automatically because climate control is on"
        return out

    def _set_opening(self, key: str, open_: bool) -> dict:
        with self._conn() as c:
            state = self.get_state()
            if open_ and any(v == "on" for v in state["hvac_zones"].values()):
                return {
                    "status": "blocked",
                    "reason": f"{key} stays closed while climate control is on",
                }
            self._set_state(c, key, "open" if open_ else "closed")
        return {"status": "success", key: "open" if open_ else "closed"}

    def set_sunroof(self, open_: bool) -> dict:
        return self._set_opening("sunroof", open_)

    def set_windows(self, open_: bool) -> dict:
        return self._set_opening("windows", open_)

    def query_status(self) -> dict:
        state = self.get_state()
        return {
            "status": "success",
            "result": {
                "cabin_temp_c": state["cabin_temp_c"],
                "hvac_zones": state["hvac_zones"],
                "zone_temp_c": state["zone_temp_c"],
                "sunroof": state["sunroof"],
                "windows": state["windows"],
                "fuel_level_pct": state["fuel_level_pct"],
                "range_km": state["range_km"],
                "battery_soc_pct": state["battery_soc_pct"],
                "engine_status": state["engine_status"],
            },
        }

    def set_reminder(self, text: str, when: str = "") -> dict:
        due = resolve_when(text, when)
        with self._conn() as c:
            c.execute(
                "INSERT INTO reminders(text,due_ts,created_at) VALUES (?,?,?)",
                (text, due, _now()),
            )
        return {
            "status": "success",
            "saved": text,
            "due_ts": due or "unspecified",
            "reminders_count": len(self.get_reminders()),
        }

    def list_reminders(self) -> dict:
        return {"status": "success", "now": _now(), "reminders": self.get_reminders()}

    def query_calendar(self) -> dict:
        return {"status": "success", "now": _now(), "events": self.get_events()}


# ---------- atomic NovaTools ----------


class SetHvacTool(NovaTool):
    name = "set_hvac"
    description = (
        "Turn a cabin HVAC/climate zone on or off, optionally setting its target temperature. "
        "Turning any zone on automatically closes (and locks closed) the sunroof and windows."
    )
    parameters = {
        "type": "object",
        "properties": {
            "zone": {
                "type": "string",
                "enum": ["driver", "passenger", "rear", "all"],
                "description": "Which HVAC zone to control, or 'all' for the whole cabin.",
            },
            "on": {"type": "boolean", "description": "True to turn the zone's climate control on, false to turn it off."},
            "target_temp_c": {
                "type": "number",
                "description": "Target temperature in Celsius (14-30). Optional; only applies when turning on.",
            },
        },
        "required": ["zone", "on"],
    }

    def __init__(self, db: VehicleDB):
        self._db = db

    def execute(self, zone: str, on: bool, target_temp_c: float | None = None) -> dict:
        return self._db.set_hvac(zone=zone, on=on, target_temp_c=target_temp_c)


class SetSunroofTool(NovaTool):
    name = "set_sunroof"
    description = (
        "Open or close the sunroof. Blocked (returns status='blocked') if any HVAC zone is "
        "currently on, since climate control keeps the sunroof closed."
    )
    parameters = {
        "type": "object",
        "properties": {"open": {"type": "boolean", "description": "True to open the sunroof, false to close it."}},
        "required": ["open"],
    }

    def __init__(self, db: VehicleDB):
        self._db = db

    def execute(self, open: bool) -> dict:  # noqa: A002 - external param name matches the schema
        return self._db.set_sunroof(open_=open)


class SetWindowsTool(NovaTool):
    name = "set_windows"
    description = (
        "Open or close the windows. Blocked (returns status='blocked') if any HVAC zone is "
        "currently on, since climate control keeps the windows closed."
    )
    parameters = {
        "type": "object",
        "properties": {"open": {"type": "boolean", "description": "True to open the windows, false to close them."}},
        "required": ["open"],
    }

    def __init__(self, db: VehicleDB):
        self._db = db

    def execute(self, open: bool) -> dict:  # noqa: A002 - external param name matches the schema
        return self._db.set_windows(open_=open)


class QueryVehicleStatusTool(NovaTool):
    name = "query_vehicle_status"
    description = (
        "Read current vehicle status: cabin/zone temperatures, HVAC zone states, sunroof/window "
        "position, fuel level, range, battery state of charge, and engine status."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, db: VehicleDB):
        self._db = db

    def execute(self) -> dict:
        return self._db.query_status()


class SetReminderTool(NovaTool):
    name = "set_reminder"
    description = (
        "Create a reminder. 'when' is a free-form natural-language time hint (e.g. 'tomorrow at "
        "4pm', 'tonight', '11:30am') resolved against the current host clock; omit it if no time "
        "was given."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind the user about."},
            "when": {"type": "string", "description": "Free-form natural-language time hint. Optional."},
        },
        "required": ["text"],
    }

    def __init__(self, db: VehicleDB):
        self._db = db

    def execute(self, text: str, when: str = "") -> dict:
        return self._db.set_reminder(text=text, when=when)


class ListRemindersTool(NovaTool):
    name = "list_reminders"
    description = ("List outstanding in-car reminders only (not Google Calendar meetings). Use check_calendar for calendar/schedule/meetings.")
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, db: VehicleDB):
        self._db = db

    def execute(self) -> dict:
        return self._db.list_reminders()


class QueryCalendarTool(NovaTool):
    name = "query_calendar"
    description = "List the user's upcoming calendar events, along with the current host-clock time."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, db: VehicleDB):
        self._db = db

    def execute(self) -> dict:
        return self._db.query_calendar()


def build_vehicle_tools(db: VehicleDB) -> list[NovaTool]:
    """Instantiate all vehicle NovaTools against one shared ``VehicleDB``."""
    return [
        SetHvacTool(db),
        SetSunroofTool(db),
        SetWindowsTool(db),
        QueryVehicleStatusTool(db),
        SetReminderTool(db),
        ListRemindersTool(db),
        QueryCalendarTool(db),
    ]
