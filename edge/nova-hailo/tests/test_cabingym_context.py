"""The agent sees the trip and the cabin as they are now, every turn, and older
requests fold into one line instead of leaving the window."""
from types import SimpleNamespace

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.context import cabin_context
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.context_history import ConversationHistory


def test_note_names_the_route_stops_results_and_a_waiting_question():
    snap = {
        c.NAV_ACTIVE: True, c.NAV_DEST: "Barbara Drive", c.NAV_ETA: 21.6,
        c.NAV_STOPS: ({"name": "Bullis Charter School", "lat": 0, "lon": 0},),
        c.NAV_RESULTS: ({"name": "Don Giovanni", "lat": 0, "lon": 0},
                        {"name": "Dar", "lat": 0, "lon": 0}),
    }
    pending = [SimpleNamespace(device="drive", api="set_mode", args={"mode": "sport"})]
    note = cabin_context(snap, pending_calls=pending,
                         recent=[("Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", 21)])
    assert "Navigating to Barbara Drive, about 22 min" in note
    assert "stops on the way: Bullis Charter School" in note
    assert "1. Don Giovanni; 2. Dar" in note
    assert "yes or no on: drive.set_mode(mode=sport)" in note
    assert "Row1 Driver Temperature -> 21" in note


def test_note_when_parked_is_short():
    assert cabin_context({c.NAV_ACTIVE: False}) == "Not navigating."


def test_cabin_keeps_the_last_three_driver_facing_changes():
    cabin = CabinEnv()
    cabin.store.set_targets({c.DRIVE_MODE: "sport"})
    cabin.store.set_targets({c.BRIGHTNESS: 40})
    cabin.store.update({c.LAT: 37.40}, source="sim")       # a sensor, not a change the driver made
    assert [p for p, _ in cabin.recent_changes] == [c.DRIVE_MODE, c.BRIGHTNESS]


def test_the_note_rides_beside_the_question_not_in_the_cached_prefix():
    h = ConversationHistory(max_turns=2)
    msgs = h.build_messages("You are Nova.", "add a stop there", state_note="Navigating to Barbara Drive.")
    assert msgs[0]["content"] == "You are Nova."
    assert "(Car now: Navigating to Barbara Drive.)" in msgs[-1]["content"]


def test_a_request_that_leaves_the_window_is_folded_not_forgotten():
    h = ConversationHistory(max_turns=2)
    h.add("add a restaurant stop in Mountain View", "Added a stop at Don Giovanni.")
    h.add("make the cabin warmer", "Driver side set to 23 degrees.")
    h.add("turn on the seat heater", "Seat heater on.")
    user = h.build_messages("You are Nova.", "why didn't you stop there?")[-1]["content"]
    assert "Earlier in this drive the driver asked: add a restaurant stop in Mountain View" in user
    assert len(h) == 2
