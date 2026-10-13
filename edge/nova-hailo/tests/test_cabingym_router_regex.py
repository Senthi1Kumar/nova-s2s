# tests/test_cabingym_router_regex.py
import pytest

from nova_hailo.cabingym.router_regex import route_vehicle_regex
from nova_hailo.cabingym.runtime import CabinEnv

HOME = {"name": "home", "lat": 37.4275, "lon": -122.1697}

DEMO = [
    ("set the temperature to 23 degrees", "hvac", "set_temperature", {"zone": "driver", "value": 23.0}),
    ("I'm a bit cold", "hvac", "set_temperature", {"zone": "driver", "delta": 1.0}),
    ("turn on the defroster", "hvac", "set_defrost", {"target": "front", "on": True}),
    ("defrost the rear window", "hvac", "set_defrost", {"target": "rear", "on": True}),
    ("turn off the AC", "hvac", "set_ac", {"on": False, "zone": "all"}),
    ("turn off the passenger side AC", "hvac", "set_ac", {"on": False, "zone": "passenger"}),
    ("switch to fresh air", "hvac", "set_recirculation", {"on": False}),
    ("sync the passenger side to mine", "hvac", "sync_zones", {}),
    ("turn on my seat heater", "seats", "set_seat_heating", {"zone": "driver", "value": 2}),
    ("open my window", "windows", "set_window", {"zone": "driver", "value": 100}),
    ("crack the rear windows", "windows", "set_window", {"zone": "rear", "value": 10}),
    ("close all the windows", "windows", "close_all_windows", {}),
    ("raise the rear shade", "windows", "set_rear_shade", {"value": 100}),
    ("lock the doors", "doors", "lock_doors", {"locked": True}),
    ("pop the trunk", "doors", "open_trunk", {}),
    ("turn off the child locks", "doors", "set_child_lock", {"on": False}),
    ("turn on the hazards", "lights", "set_hazards", {"on": True}),
    ("high beams on", "lights", "set_headlights", {"mode": "high"}),
    ("turn on the reading lights in the back", "lights", "set_interior_light", {"zone": "rear", "on": True}),
    ("make the ambient lighting blue", "lights", "set_ambient_light", {"color": "blue"}),
    ("open the sunroof", "body", "set_sunroof", {"value": 100}),
    ("tilt the sunroof", "body", "set_sunroof", {"value": -100}),
    ("turn the wipers on", "body", "set_wipers", {"mode": "low"}),
    ("fold the mirrors", "body", "fold_mirrors", {"folded": True}),
    ("close the sunshade", "body", "set_sunshade", {"value": 100}),
    ("play some jazz", "media", "play_media", {"query": "jazz"}),
    ("pause the music", "media", "pause_media", {}),
    ("skip this song", "media", "next_track", {}),
    ("turn the volume up", "media", "set_volume", {"delta": 10}),
    ("switch to the radio", "media", "set_source", {"source": "radio"}),
    ("take me home", "navigation", "navigate_to", {"destination": HOME}),
    ("cancel navigation", "navigation", "stop_navigation", {}),
    ("call Sam", "phone", "call_contact", {"name": "Sam"}),
    ("answer it", "phone", "answer_call", {}),
    ("decline", "phone", "reject_call", {}),
    ("hang up", "phone", "end_call", {}),
    ("read my messages", "phone", "read_messages", {}),
    ("text Priya that I'm on my way", "phone", "send_message", {"recipient": "Priya", "text": "I'm on my way"}),
    ("who called me", "phone", "list_missed_calls", {}),
    ("how much range do I have left", "drive", "get_range", {}),
    ("what gear am I in", "drive", "get_vehicle_status", {"signal": "gear"}),
    ("where am I", "drive", "get_location", {}),
    ("what's the temperature outside", "cabin", "get_outside_temperature", {}),
    ("switch to sport mode", "system", "set_drive_mode", {"mode": "sport"}),
    ("dim the screen", "system", "set_display_brightness", {"delta": -10}),
]


@pytest.mark.parametrize("text,device,api,args", DEMO)
def test_demo_utterances(text, device, api, args):
    out = route_vehicle_regex(text, CabinEnv())
    assert out is not None and out.kind == "vehicle", text
    (call,) = out.calls
    assert (call.device, call.api, call.args) == (device, api, args)
    assert out.trace["router"] == "regex"


def test_multi_intent():
    out = route_vehicle_regex("make it warmer for me and call Sam", CabinEnv())
    assert [(x.device, x.api) for x in out.calls] == [("hvac", "set_temperature"), ("phone", "call_contact")]


def test_numbers_stay_in_their_clause():
    out = route_vehicle_regex("set the temperature to 21 and the volume to 30", CabinEnv())
    assert [(x.api, x.args.get("value")) for x in out.calls] == [("set_temperature", 21.0), ("set_volume", 30)]


def test_unknown_contact_and_place_clarify():
    out = route_vehicle_regex("call Bob", CabinEnv())
    assert out.kind == "clarify" and "contacts" in out.speak
    assert route_vehicle_regex("navigate to the moon base", CabinEnv()).kind == "clarify"


def test_rear_seat_declines():
    out = route_vehicle_regex("heat the rear seats", CabinEnv())
    assert out.kind == "decline" and "rear" in out.speak


def test_find_nearby_only_when_poi_search_allowed():
    out = route_vehicle_regex("find the nearest charging station", CabinEnv())
    assert out.calls[0].api == "find_nearby"
    assert route_vehicle_regex("find the nearest charging station", CabinEnv(live=True)) is None


@pytest.mark.parametrize("text", [
    "find coffee shops in Bangalore", "search for coffee near me", "what's the weather",
    "what's the latest news", "check my calendar", "what's next on my calendar", "tell me a joke",
    "who is the prime minister of India", "what is the stock price of Nvidia", "what's the temperature",
    "hello there",
])
def test_regex_router_leaves_web_queries_alone(text):
    assert route_vehicle_regex(text, CabinEnv(live=True)) is None


def _apis(text, cabin=None):
    out = route_vehicle_regex(text, cabin or CabinEnv())
    return [(call.device, call.api) for call in (out.calls if out else [])], out


def test_picking_someone_up_somewhere_is_a_stop_not_a_phone_call():
    for text in ("I would like to pick up my child at the Hamilton Ave Chargers on the way",
                 "add a new stop at Blue Door Coffee to pick up my children",
                 "pick up my kids at Blue Door Coffee"):
        calls, out = _apis(text)
        assert ("phone", "answer_call") not in calls, text
        assert out is not None and (out.kind == "clarify" or ("navigation", "add_stop") in calls), text


def test_answering_a_call_still_works():
    for text in ("answer the call", "pick it up", "accept the call", "pick up the phone", "answer"):
        calls, _ = _apis(text)
        assert calls == [("phone", "answer_call")], text


def test_the_pickup_place_becomes_the_stop():
    from nova_hailo.cabingym import catalog as c
    from nova_hailo.cabingym.executor import VehicleExecutor

    for text in ("pick up my kids at Blue Door Coffee on the way",
                 "add a new stop at Blue Door Coffee to pick up my children",
                 "drop my son off at Blue Door Coffee"):
        cab = CabinEnv()
        cab.store.apply({c.NAV_ACTIVE: True, c.NAV_DEST: "home", c.NAV_DEST_LAT: 37.4275,
                         c.NAV_DEST_LON: -122.1697})
        out = VehicleExecutor(cab).run(route_vehicle_regex(text, cab))
        assert out["status"] == "success", (text, out.get("speak"))
        assert [s["name"] for s in cab.store.get(c.NAV_STOPS)] == ["Blue Door Coffee"], text
