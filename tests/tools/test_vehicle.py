"""Real-SQLite tests for the vehicle NovaTools (no mocking).

Each test uses a fresh temp-file DB (``tmp_path``-scoped) so tests are
isolated from each other and from the real ``runtime/vehicle.db``.
"""
from __future__ import annotations

import pytest

from nova.tools.vehicle import (
    ListRemindersTool,
    QueryCalendarTool,
    QueryVehicleStatusTool,
    SetHvacTool,
    SetReminderTool,
    SetSunroofTool,
    SetWindowsTool,
    VehicleDB,
    build_vehicle_tools,
)


@pytest.fixture
def db(tmp_path):
    return VehicleDB(tmp_path / "vehicle.db")


def test_set_hvac_then_query_reflects_change(db):
    hvac = SetHvacTool(db)
    status = QueryVehicleStatusTool(db)

    result = hvac.execute(zone="driver", on=True, target_temp_c=19)
    assert result["status"] == "success"

    read_back = status.execute()
    assert read_back["status"] == "success"
    assert read_back["result"]["hvac_zones"]["driver"] == "on"
    assert read_back["result"]["zone_temp_c"]["driver"] == 19.0


def test_hvac_interlock_blocks_sunroof_while_on(db):
    hvac = SetHvacTool(db)
    sunroof = SetSunroofTool(db)

    hvac.execute(zone="all", on=True, target_temp_c=22)
    result = sunroof.execute(open=True)

    assert result["status"] == "blocked"
    status = QueryVehicleStatusTool(db).execute()
    assert status["result"]["sunroof"] == "closed"


def test_hvac_interlock_auto_closes_open_sunroof(db):
    sunroof = SetSunroofTool(db)
    windows = SetWindowsTool(db)
    hvac = SetHvacTool(db)

    # Open sunroof/windows while HVAC is off - should succeed.
    assert sunroof.execute(open=True)["status"] == "success"
    assert windows.execute(open=True)["status"] == "success"
    status = QueryVehicleStatusTool(db).execute()
    assert status["result"]["sunroof"] == "open"
    assert status["result"]["windows"] == "open"

    # Turning HVAC on should auto-close both.
    result = hvac.execute(zone="driver", on=True)
    assert set(result["auto_closed"]) == {"sunroof", "windows"}

    status = QueryVehicleStatusTool(db).execute()
    assert status["result"]["sunroof"] == "closed"
    assert status["result"]["windows"] == "closed"


def test_set_reminder_then_list_reminders(db):
    set_tool = SetReminderTool(db)
    list_tool = ListRemindersTool(db)

    result = set_tool.execute(text="pick up dry cleaning", when="tomorrow at 4pm")
    assert result["status"] == "success"
    assert result["saved"] == "pick up dry cleaning"
    assert result["due_ts"] != "unspecified"

    listed = list_tool.execute()
    assert listed["status"] == "success"
    assert any(r["text"] == "pick up dry cleaning" for r in listed["reminders"])


def test_query_calendar_returns_seeded_demo_events(db):
    calendar = QueryCalendarTool(db)
    result = calendar.execute()
    assert result["status"] == "success"
    titles = {e["title"] for e in result["events"]}
    assert "Team standup" in titles
    assert "Elevatex AI review" in titles


def test_persistence_across_tool_instances_sharing_one_db_file(tmp_path):
    db_path = tmp_path / "vehicle.db"
    db1 = VehicleDB(db_path)
    SetHvacTool(db1).execute(zone="rear", on=True, target_temp_c=24)

    # A fresh VehicleDB instance pointed at the same file sees the write.
    db2 = VehicleDB(db_path)
    status = QueryVehicleStatusTool(db2).execute()
    assert status["result"]["hvac_zones"]["rear"] == "on"
    assert status["result"]["zone_temp_c"]["rear"] == 24.0


@pytest.mark.parametrize(
    "tool_cls",
    [
        SetHvacTool,
        SetSunroofTool,
        SetWindowsTool,
        QueryVehicleStatusTool,
        SetReminderTool,
        ListRemindersTool,
        QueryCalendarTool,
    ],
)
def test_function_tool_schema_well_formed(db, tool_cls):
    tool = tool_cls(db)
    schema = tool.to_function_tool()

    assert schema["type"] == "function"
    assert schema["name"] == tool.name
    assert isinstance(schema["name"], str) and schema["name"]
    assert isinstance(schema["description"], str) and schema["description"]
    assert isinstance(schema["parameters"], dict)
    assert schema["parameters"]["type"] == "object"
    assert "properties" in schema["parameters"]


def test_build_vehicle_tools_returns_all_seven(db):
    tools = build_vehicle_tools(db)
    names = {t.name for t in tools}
    assert names == {
        "set_hvac",
        "set_sunroof",
        "set_windows",
        "query_vehicle_status",
        "set_reminder",
        "list_reminders",
        "query_calendar",
    }
