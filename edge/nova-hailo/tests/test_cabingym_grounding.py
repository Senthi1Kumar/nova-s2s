# tests/test_cabingym_grounding.py
import pytest

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.grounding import (
    NONE_OF_THESE, clarify_speak, decline_speak, entity_names, ground_args, normalize_cloud_args,
)
from nova_hailo.cabingym.runtime import CabinEnv


@pytest.fixture
def env():
    return CabinEnv()


def api(env, dev, name):
    return env.devices[dev].apis[name]


def test_cabin_env_basics(env):
    assert env.profile.powertrain == "ev" and not env.hybrid
    assert env.allow_poi_search
    assert CabinEnv(live=True).allow_poi_search is False
    assert set(env.summary()) == {"speed_kph", "gear", "call_state", "nav_active", "media_state"}
    assert env.context().powertrain == "ev"


def test_adapter_choice():
    with pytest.raises(ValueError):
        CabinEnv(adapter="hybrid")

    class FakeHybrid:
        name = "hybrid"

        def attach(self, store):
            store.target_sink = None

        def tick(self, dt_s):
            return []

        def settle(self):
            return []

    assert CabinEnv(adapter=FakeHybrid()).hybrid


def test_temperature_value_and_default_zone(env):
    g = ground_args(api(env, "hvac", "set_temperature"), "set the temperature to 23 degrees", env)
    assert g.ok()
    assert g.args == {"zone": "driver", "value": 23.0}
    assert g.grounding == {"zone": "default", "value": "parser"}


def test_temperature_relative(env):
    g = ground_args(api(env, "hvac", "set_temperature"), "make it a bit warmer for me", env)
    assert g.args == {"zone": "driver", "delta": 1.0}
    assert g.grounding["zone"] == "parser"


def test_missing_value(env):
    g = ground_args(api(env, "hvac", "set_temperature"), "set the temperature", env)
    assert g.missing == ["value|delta"]


def test_unsupported_rear_seat(env):
    g = ground_args(api(env, "seats", "set_seat_heating"), "heat the rear seats", env)
    assert g.unsupported == ["zone"]
    assert "rear" in decline_speak(api(env, "seats", "set_seat_heating"), g.unsupported)


def test_enum_default_and_bool(env):
    g = ground_args(api(env, "hvac", "set_defrost"), "turn on the defroster", env)
    assert g.args == {"target": "front", "on": True}
    assert g.grounding == {"target": "default", "on": "parser"}


def test_contacts(env):
    call = api(env, "phone", "call_contact")
    assert ground_args(call, "call Sam", env).args == {"name": "Sam"}
    g = ground_args(call, "call Bob", env)
    assert g.missing == ["name"]
    assert "contacts" in clarify_speak(call, g.missing)
    assert ground_args(call, "call her", env, choose=lambda p: "Priya").args == {"name": "Priya"}
    assert ground_args(call, "call Sam", env, choose=lambda p: NONE_OF_THESE).missing == ["name"]


def test_places(env):
    nav = api(env, "navigation", "navigate_to")
    assert ground_args(nav, "take me home", env).args["destination"] == {
        "name": "home", "lat": 37.4275, "lon": -122.1697}
    g = ground_args(nav, "navigate to Sam's place", env)
    assert g.args["destination"]["name"] == "Sam's place" and g.grounding["destination"] == "entity"
    stop = api(env, "navigation", "add_stop")
    g = ground_args(stop, "add a stop at the nearest coffee shop", env)
    assert g.args["destination"]["name"] == "Hilltop Cafe" and g.grounding["destination"] == "span"
    live = CabinEnv(live=True)
    assert ground_args(live.devices["navigation"].apis["add_stop"],
                       "add a stop at the nearest coffee shop", live).missing == ["destination"]
    assert ground_args(nav, "navigate to the moon base", env).missing == ["destination"]


def test_message_span_and_count(env):
    g = ground_args(api(env, "phone", "send_message"), "text Priya that I'm on my way", env)
    assert g.args == {"recipient": "Priya", "text": "I'm on my way"}
    g = ground_args(api(env, "phone", "read_messages"), "read my last two messages", env)
    assert g.args == {"count": 2}
    assert entity_names("senders", env.store.snapshot()) == ["Priya", "Sam"]


def test_cloud_args(env):
    temp = api(env, "hvac", "set_temperature")
    g = normalize_cloud_args(temp, {"zone": "driver", "value": 21}, env)
    assert g.args == {"zone": "driver", "value": 21.0}
    assert g.grounding == {"zone": "cloud", "value": "cloud"}
    assert normalize_cloud_args(temp, {"zone": "rear", "value": 21}, env).unsupported == ["zone"]
    assert normalize_cloud_args(temp, {"zone": "moon", "value": 21}, env).missing == ["zone"]
    fan = normalize_cloud_args(api(env, "hvac", "set_fan"), {"value": 3}, env)
    assert fan.args == {"value": 3.0, "zone": "all"} and fan.grounding["zone"] == "default"
    call = api(env, "phone", "call_contact")
    assert normalize_cloud_args(call, {"name": "sam"}, env).args == {"name": "Sam"}
    assert normalize_cloud_args(call, {"name": "Bob"}, env).missing == ["name"]
    dest = normalize_cloud_args(api(env, "navigation", "navigate_to"), {"destination": "Home"}, env)
    assert dest.args["destination"]["name"] == "home"
    assert normalize_cloud_args(api(env, "doors", "lock_doors"), {"locked": "false"}, env).args == {
        "locked": False}
