# tests/test_cabingym_devices_body.py
import re

import pytest

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices import body, doors, hvac, lights, seats, windows
from nova_hailo.cabingym.devices.base import ApiContext
from nova_hailo.cabingym.nav import FixtureNav
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall

DEVICES = [hvac.DEVICE, seats.DEVICE, windows.DEVICE, doors.DEVICE, lights.DEVICE, body.DEVICE]


def run(dev, api, args, **init):
    store = c.build_store()
    if init:
        store.apply({c.resolve(k): v for k, v in init.items()})
    return dev.apis[api].handler(ApiContext(store.snapshot(), FixtureNav(), "ev"), args)


def test_api_metadata_is_consistent():
    paths = {s.path for s in c.SIGNALS}
    for dev in DEVICES:
        for api in dev.apis.values():
            assert set(api.writes) <= paths, (dev.name, api.name)
            for pname, spec in api.params.items():
                assert "x-ground" in spec, (dev.name, api.name, pname)
            schema = api.json_schema()
            assert all(not k.startswith("x-") for p in schema["properties"].values() for k in p)
            for pat in api.patterns:
                re.compile(pat)


def test_vehicle_call_round_trip():
    call = VehicleCall("hvac", "set_temperature", {"value": 21.0}, {"value": "parser"}, 0.9)
    assert call.to_dict() == {"device": "hvac", "api": "set_temperature", "args": {"value": 21.0},
                              "grounding": {"value": "parser"}, "score": 0.9}
    assert RouteOutcome("clarify", speak="Who?").calls == []


@pytest.mark.parametrize("args,init,targets,speak", [
    ({"zone": "driver", "value": 23.0}, {}, {"hvac.driver.temp": 23.0}, "Driver side set to 23 degrees."),
    ({"zone": "driver", "delta": 1.0}, {"hvac.driver.temp": 20.0}, {"hvac.driver.temp": 21.0}, None),
    ({"zone": "driver", "value": 35.0}, {}, {"hvac.driver.temp": 28.0}, "Driver side set to 28 degrees. That's the limit."),
    ({"zone": "all", "value": 20.0}, {}, {"hvac.driver.temp": 20.0, "hvac.passenger.temp": 20.0}, "Both sides set to 20 degrees."),
])
def test_set_temperature(args, init, targets, speak):
    res = run(hvac.DEVICE, "set_temperature", args, **init)
    assert res.targets == {c.resolve(k): v for k, v in targets.items()}
    if speak:
        assert res.speak == speak


def test_fan_and_flags():
    res = run(hvac.DEVICE, "set_fan", {"zone": "all", "delta": 1})
    assert res.targets == {c.hvac_fan("Driver"): 3, c.hvac_fan("Passenger"): 3}
    assert res.speak == "Fan set to 3."
    assert run(hvac.DEVICE, "set_fan", {"zone": "all", "value": 0}).speak == "Fan off."
    assert run(hvac.DEVICE, "set_ac", {"on": False}).targets == {c.AC: False}
    assert run(hvac.DEVICE, "set_defrost", {"target": "rear", "on": True}).targets == {c.DEFROST_REAR: True}
    assert run(hvac.DEVICE, "set_recirculation", {"on": False}).speak == "Fresh air on."
    res = run(hvac.DEVICE, "sync_zones", {}, **{"hvac.driver.temp": 22.0})
    assert res.targets == {c.hvac_temp("Passenger"): 22.0, c.HVAC_SYNC: True}


def test_seats():
    res = run(seats.DEVICE, "set_seat_heating", {"zone": "passenger", "value": 3})
    assert res.targets == {c.resolve("seat.passenger.heat"): 3}
    assert res.speak == "Passenger seat heating on high."
    res = run(seats.DEVICE, "set_seat_heating", {"zone": "driver", "value": 0}, **{"seat.driver.heat": 2})
    assert res.speak == "Driver seat heating off."
    res = run(seats.DEVICE, "set_seat_ventilation", {"zone": "driver", "delta": 1})
    assert res.targets == {c.resolve("seat.driver.vent"): 1}
    assert run(seats.DEVICE, "set_massage", {"zone": "driver", "on": True}).targets == {
        c.resolve("seat.driver.massage"): True}


def test_windows():
    res = run(windows.DEVICE, "set_window", {"zone": "rear", "value": 10})
    assert res.targets == {c.resolve("window.rear_left"): 10, c.resolve("window.rear_right"): 10}
    assert res.speak == "Rear windows opened to 10 percent."
    assert run(windows.DEVICE, "set_window", {"zone": "driver", "value": 100}).speak == "Driver window open."
    res = run(windows.DEVICE, "close_all_windows", {})
    assert set(res.targets.values()) == {0} and len(res.targets) == 4
    assert run(windows.DEVICE, "set_rear_shade", {"value": 100}).targets == {c.REAR_SHADE: 100}


def test_doors():
    res = run(doors.DEVICE, "lock_doors", {"locked": False})
    assert set(res.targets.values()) == {False} and len(res.targets) == 4
    assert res.speak == "Doors unlocked."
    assert run(doors.DEVICE, "open_trunk", {}).targets == {c.TRUNK: True}
    res = run(doors.DEVICE, "set_child_lock", {"on": False})
    assert res.targets == {c.resolve("child_lock.rear_left"): False,
                           c.resolve("child_lock.rear_right"): False}


def test_lights():
    res = run(lights.DEVICE, "set_headlights", {"mode": "high"})
    assert res.targets == {c.HEADLIGHTS: "high"} and res.speak == "High beams on."
    assert run(lights.DEVICE, "set_interior_light", {"zone": "rear", "on": True}).targets == {
        c.REAR_READING: True}
    assert run(lights.DEVICE, "set_ambient_light", {"color": "blue"}).targets == {c.AMBIENT_COLOR: "blue"}
    assert run(lights.DEVICE, "set_hazards", {"on": True}).targets == {c.HAZARDS: True}


def test_body():
    assert run(body.DEVICE, "set_sunroof", {"value": -100}).speak == "Sunroof tilted."
    assert run(body.DEVICE, "set_sunroof", {"value": 100}).targets == {c.SUNROOF: 100}
    assert run(body.DEVICE, "set_wipers", {"mode": "low"}).speak == "Wipers on."
    assert run(body.DEVICE, "fold_mirrors", {"folded": True}).targets == {c.MIRRORS_FOLDED: True}
    assert run(body.DEVICE, "set_sunshade", {"value": 100}).speak == "Sunshade closed."
