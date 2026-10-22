# tests/test_cabingym_devices_info.py
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices import DEVICE_ORDER, build_devices
from nova_hailo.cabingym.devices.base import ApiContext
from nova_hailo.cabingym.nav import FixtureNav


def run(dev_name, api, args, profile="ev_demo", **init):
    store = c.build_store(profile)
    if init:
        store.apply({c.resolve(k): v for k, v in init.items()})
    dev = build_devices()[dev_name]
    power = c.get_profile(profile).powertrain
    return dev.apis[api].handler(ApiContext(store.snapshot(), FixtureNav(), power), args)


def test_registry_has_twelve_devices_and_57_apis():
    devs = build_devices()
    assert tuple(devs) == DEVICE_ORDER and len(devs) == 12
    assert sum(len(d.apis) for d in devs.values()) == 57


def test_media():
    res = run("media", "play_media", {"query": "jazz"})
    assert res.targets == {c.MEDIA_STATE: "playing", c.MEDIA_TITLE: "jazz", c.MEDIA_TRACK: 0}
    assert run("media", "pause_media", {}, **{"media.state": "playing"}).targets == {
        c.MEDIA_STATE: "paused"}
    assert run("media", "pause_media", {}).targets == {}
    assert run("media", "next_track", {}, **{"media.state": "playing"}).targets == {
        c.MEDIA_TRACK: 1, c.MEDIA_STATE: "playing", c.MEDIA_TITLE: ""}
    assert run("media", "set_volume", {"delta": 10}).targets == {c.VOLUME: 50}
    assert run("media", "set_volume", {"value": 0}).speak == "Muted."
    assert run("media", "set_source", {"source": "radio"}).targets == {c.MEDIA_SOURCE: "radio"}


def test_navigation():
    home = {"name": "home", "lat": 37.4275, "lon": -122.1697}
    res = run("navigation", "navigate_to", {"destination": home})
    assert res.targets[c.NAV_ACTIVE] is True and res.targets[c.NAV_DEST] == "home"
    assert res.speak.startswith("Navigating to home. About ")
    assert run("navigation", "add_stop", {"destination": home}).status == "clarify"
    res = run("navigation", "stop_navigation", {}, **{"nav.active": True, "nav.dest": "home"})
    assert res.targets[c.NAV_ACTIVE] is False and res.targets[c.NAV_DEST] == ""
    res = run("navigation", "find_nearby", {"category": "charging"})
    assert res.speak.startswith("The closest charging station is ")
    assert len(res.targets[c.NAV_RESULTS]) == 2


def test_navigation_add_stop_keeps_destination():
    stop = {"name": "Hilltop Cafe", "lat": 37.4430, "lon": -122.1580, "category": "coffee"}
    res = run("navigation", "add_stop", {"destination": stop},
              **{"nav.active": True, "nav.dest": "home", "nav.dest_lat": 37.4275,
                 "nav.dest_lon": -122.1697})
    assert res.targets[c.NAV_STOPS] == (stop,)
    assert res.targets[c.NAV_DEST] == "home"


def test_phone_calls():
    res = run("phone", "call_contact", {"name": "Sam"})
    assert res.targets == {c.CALL_STATE: "dialing", c.CALL_PEER: "Sam", c.LAST_DIALED: "Sam"}
    assert run("phone", "call_contact", {"name": "Sam"}, **{"call.state": "active"}).status == "clarify"
    assert run("phone", "answer_call", {}).status == "clarify"
    ringing = {"call.state": "ringing", "call.peer": "Priya"}
    assert run("phone", "answer_call", {}, **ringing).targets == {c.CALL_STATE: "active"}
    assert run("phone", "reject_call", {}, **ringing).targets == {c.CALL_STATE: "idle", c.CALL_PEER: ""}
    assert run("phone", "end_call", {}, **{"call.state": "active", "call.peer": "Sam"}).targets == {
        c.CALL_STATE: "idle", c.CALL_PEER: "", c.CALL_MUTED: False}
    assert run("phone", "redial", {}).status == "clarify"


def test_phone_messages():
    res = run("phone", "read_messages", {})
    assert res.targets == {c.READ_IDS: ("m2",)}
    assert res.speak == "Sam says: Can you pick up milk on the way home?"
    res = run("phone", "read_messages", {"sender": "Priya", "_truncate": True})
    assert res.targets == {c.READ_IDS: ("m1",)}
    assert res.speak.endswith("…") or len(res.speak.split()) <= 15
    assert run("phone", "read_messages", {}, **{"read_ids": ["m1", "m2"]}).speak == "You have no new messages."
    res = run("phone", "send_message", {"recipient": "Priya", "text": "I'm on my way"})
    assert res.targets == {c.OUTBOX: ({"to": "Priya", "text": "I'm on my way"},)}
    assert run("phone", "list_missed_calls", {}).speak == "You missed a call from Alex."


def test_drive_reads_follow_powertrain():
    assert run("drive", "get_range", {}).speak == "About 210 kilometres of range, 68 percent charge."
    assert run("drive", "get_range", {}, profile="audi_q7_ice").speak == (
        "About 520 kilometres of range, 55 percent fuel.")
    assert run("drive", "get_vehicle_status", {"signal": "speed"}, speed=62.0).speak == (
        "You're doing 62 kilometres an hour.")
    assert run("drive", "get_location", {}, lat=37.4275, lon=-122.1697).speak == "You're at home."
    # No GPS fix: say the position is the simulation's, by its area name, not coordinates.
    named = run("drive", "get_location", {}, **{"gps.fix": False, "place": "Palo Alto", "heading": 0})
    assert named.speak == "No GPS fix right now. The simulated car is in Palo Alto, heading north."
    assert "37." not in named.speak
    assert run("drive", "get_range", {}).targets == {}


def test_location_reverse_geocodes_only_when_the_area_is_unknown(monkeypatch):
    import nova_hailo.cabingym.devices.drive as drive

    monkeypatch.setattr(drive, "reverse_place_name", lambda lat, lon: "Old Palo Alto")
    store = c.build_store("ev_demo")
    snap = dict(store.snapshot())
    snap[c.PLACE] = ""
    snap[c.GPS_FIX] = False
    snap[c.HEADING] = 0
    res = build_devices()["drive"].apis["get_location"].handler(
        ApiContext(snap, FixtureNav(), "ev"), {})
    assert res.speak == "No GPS fix right now. The simulated car is in Old Palo Alto, heading north."
    assert res.targets[c.PLACE] == "Old Palo Alto"
    monkeypatch.setattr(drive, "reverse_place_name", lambda lat, lon: None)
    bare = build_devices()["drive"].apis["get_location"].handler(
        ApiContext(snap, FixtureNav(), "ev"), {})
    assert bare.speak.startswith("No GPS fix right now. The simulated car is at 37.4419")


def test_with_a_fix_where_am_i_names_the_street(monkeypatch):
    import nova_hailo.cabingym.devices.drive as drive

    monkeypatch.setattr(drive, "reverse_street", lambda lat, lon: "on El Camino Real in Mountain View")
    store = c.build_store("ev_demo")
    snap = dict(store.snapshot())
    snap.update({c.GPS_FIX: True, c.LAT: 37.39, c.LON: -122.08, c.HEADING: 0, c.PLACE: "Mountain View"})
    res = build_devices()["drive"].apis["get_location"].handler(ApiContext(snap, FixtureNav(), "ev"), {})
    assert res.speak == "You're on El Camino Real in Mountain View, heading north."
    monkeypatch.setattr(drive, "reverse_street", lambda lat, lon: None)   # offline: the area
    res = build_devices()["drive"].apis["get_location"].handler(ApiContext(snap, FixtureNav(), "ev"), {})
    assert res.speak == "You're in Mountain View, heading north."


def test_street_from_a_mapbox_reverse_answer():
    from nova_hailo.cabingym.adapters.gps_nmea import street_place

    address = {"features": [{"properties": {"feature_type": "address", "name": "1900 El Camino Real",
                                            "context": {"street": {"name": "El Camino Real"},
                                                        "place": {"name": "Mountain View"}}}}]}
    assert street_place(address) == "on El Camino Real in Mountain View"
    street = {"features": [{"properties": {"feature_type": "street", "name": "Castro Street",
                                           "context": {"place": {"name": "Mountain View"}}}}]}
    assert street_place(street) == "on Castro Street in Mountain View"
    assert street_place({"features": []}) is None


def test_the_cloud_prompt_says_when_the_position_is_simulated():
    from nova_hailo.cabingym.nav import cloud_location_sentence

    real = cloud_location_sentence("Mountain View", 37.39, -122.08, gps_fix=True)
    assert real.startswith("The car is near Mountain View (37.3900, -122.0800).")
    sim = cloud_location_sentence("Palo Alto", 37.44, -122.16, gps_fix=False)
    assert sim.startswith("There is no GPS fix: the car's position is simulated, near Palo Alto")


def test_cabin_and_system():
    assert run("cabin", "get_outside_temperature", {}).speak == "It's 18 degrees outside."
    assert run("cabin", "get_occupancy", {}).speak == "Just you, in the driver's seat."
    assert run("system", "set_drive_mode", {"mode": "sport"}).targets == {c.DRIVE_MODE: "sport"}
    assert run("system", "set_display_brightness", {"delta": -10}).targets == {c.BRIGHTNESS: 60}
    assert run("system", "get_connectivity", {}, connectivity="none").speak.startswith("I'm offline")
