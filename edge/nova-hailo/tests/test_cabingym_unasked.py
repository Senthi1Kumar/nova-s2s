"""A cabin change the driver did not ask for is confirmed, not made.

Right after "not this Barbara Drive, stop the navigation", the cloud model answered
"What I said was the Barbara Drive in Las Gados" by turning the A/C on.
"""
from nova_hailo.cabingym import catalog as c
from nova_hailo.tools.oem_tools import OemToolGateway


def _gateway(utterance, dialog=()):
    g = OemToolGateway(enabled=["vehicle"], router="regex", jev_warm=False)
    g.cabin.dialog = [{"user": u, "assistant": ""} for u in dialog]
    g._cabin_utterance = utterance
    return g


def test_an_ac_change_during_a_place_correction_is_confirmed_first():
    g = _gateway("What I said was the Barbara Drive in Las Gados.",
                 ["Hey, not this barbara drag stop the navigation."])
    before = g.cabin.store.get(c.AC)
    out = g.execute("vehicle_hvac", action="set_ac", on=not before)
    assert out["status"] == "confirm"
    assert g.cabin.store.get(c.AC) is before
    assert g.vehicle.resolve_confirm(True)["status"] == "success"
    assert g.cabin.store.get(c.AC) is (not before)


def test_a_request_that_names_the_control_goes_straight_through():
    g = _gateway("turn on the AC", ["take me to Kirk's"])
    assert g.execute("vehicle_hvac", action="set_ac", on=True)["status"] == "success"


def test_a_follow_up_on_the_same_control_goes_straight_through():
    g = _gateway("set it to one speed.", ["And turn on the AC and set the fan speed to one."])
    assert g.execute("vehicle_hvac", action="set_fan", value=1)["status"] == "success"


def test_a_feeling_with_no_control_named_goes_straight_through():
    g = _gateway("I'm freezing")
    assert g.execute("vehicle_hvac", action="set_temperature", delta=1)["status"] == "success"


def test_a_request_about_another_control_is_confirmed():
    g = _gateway("open the sunroof a little")
    assert g.execute("vehicle_hvac", action="set_ac", on=True)["status"] == "confirm"
