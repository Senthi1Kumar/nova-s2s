"""Follow-up cabin commands keep the zone the driver already named.

Jev sees the cabin store and the recent dialogue. Code still refuses to
write a follow-up onto the driver when the passenger was the subject.
"""
import pytest

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.router_jev import route_vehicle_jev, stage2_questions
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.tools.oem_tools import OemToolGateway


class FakeJev:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def system_one(self, state, questions, timeout=None):
        self.calls.append(state)
        if not self.answers:
            raise TimeoutError("no answer")
        return {"answers": self.answers.pop(0)}


def n(p):
    return {"noul": p}


def ch(v):
    return {"choice": v}


def test_zone_question_reads_dialog_focus_and_vehicle():
    text = stage2_questions(["hvac"], CabinEnv())["hvac.set_temperature.zone"]["instructions"]["inspect"]
    assert "dialog" in text and "focus" in text and "vehicle" in text


def test_followup_keeps_the_passenger_zone_when_jev_picks_the_default():
    cabin = CabinEnv()
    cabin.dialog = [{"user": "decrease the passenger side temperature", "assistant": "Which temperature?"}]
    jev = FakeJev(
        {"dev.hvac": n(0.95)},
        {"hvac.set_temperature": n(0.93), "hvac.set_temperature.zone": ch("driver")},
    )
    out = route_vehicle_jev("yes, set to twenty degrees", cabin, client=jev, dialog=cabin.dialog)
    assert out.calls[0].args["zone"] == "passenger"
    assert out.calls[0].args["value"] == 20.0
    assert out.calls[0].grounding["zone"] == "dialog"
    state = jev.calls[0]
    assert state["vehicle"]["hvac"]["passenger_c"] == cabin.store.get(c.hvac_temp("Passenger"))
    assert state["dialog"][0]["user"].startswith("decrease the passenger")
    assert "ambient_color" in state["vehicle"]["lights"]


def test_take_cabin_turn_writes_the_passenger_and_leaves_navigation():
    jev = FakeJev(
        {"dev.hvac": n(0.95)},
        {"hvac.set_temperature": n(0.93), "hvac.set_temperature.zone": ch("driver")},
    )
    g = OemToolGateway(enabled=["vehicle"], router="jev", jev_client=jev, jev_warm=False)
    g.cabin.dialog = [{"user": "the passenger side is too warm", "assistant": "What temperature?"}]
    out = g.take_cabin_turn("yes, set to twenty degrees", dialog=g.cabin.dialog)
    assert "20" in out["speak"]
    assert g.cabin.store.get(c.hvac_temp("Passenger")) == 20.0
    assert g.cabin.store.get(c.hvac_temp("Driver")) == 21.0
    assert g.cabin.focus["zone"] == "passenger"

    nav = FakeJev({"dev.navigation": n(0.99)})
    g2 = OemToolGateway(enabled=["vehicle"], router="jev", jev_client=nav, jev_warm=False)
    assert g2.take_cabin_turn("take me to Los Gatos") is None
    assert nav.calls == []


def test_cloud_call_without_a_zone_uses_the_passenger_from_the_dialogue():
    g = OemToolGateway(enabled=["vehicle"], router="regex", jev_warm=False)
    g.cabin.dialog = [{"user": "passenger side please", "assistant": ""}]
    g._cabin_utterance = "yes, set to twenty degrees"
    out = g.execute("vehicle_hvac", action="set_temperature", value=20)
    assert out["status"] == "success"
    assert g.cabin.store.get(c.hvac_temp("Passenger")) == 20.0
    assert g.cabin.store.get(c.hvac_temp("Driver")) == 21.0


@pytest.mark.parametrize("cloud_zone", [None, "driver"])
def test_a_side_said_for_the_seat_heater_does_not_carry_to_the_fan(cloud_zone):
    """"turn the seat on driver side only", then "set it to one speed." set only the
    driver's fan, so the passenger fan stayed at 3 and the cockpit kept showing 3."""
    g = OemToolGateway(enabled=["vehicle"], router="regex", jev_warm=False)
    g.cabin.store.update({c.hvac_fan("Driver"): 3, c.hvac_fan("Passenger"): 3})
    g.cabin.dialog = [{"user": "I'm freezing now can you turn the seat on driver side only?",
                       "assistant": "Driver seat heating on medium."},
                      {"user": "Set the heat to low.", "assistant": "Driver seat heating on low."},
                      {"user": "And turn on the AC and set the fan speed to one.",
                       "assistant": "Air conditioning on. Fan set to 3."}]
    g._cabin_utterance = "set it to one speed."
    kw = {"zone": cloud_zone} if cloud_zone else {}
    out = g.execute("vehicle_hvac", action="set_fan", value=1, **kw)
    assert out["status"] == "success"
    assert g.cabin.store.get(c.hvac_fan("Driver")) == 1
    assert g.cabin.store.get(c.hvac_fan("Passenger")) == 1


def test_a_side_said_for_the_same_control_still_carries():
    g = OemToolGateway(enabled=["vehicle"], router="regex", jev_warm=False)
    g.cabin.store.update({c.hvac_fan("Driver"): 3, c.hvac_fan("Passenger"): 3})
    g.cabin.dialog = [{"user": "turn the passenger fan down", "assistant": "Passenger fan set to 2."}]
    g._cabin_utterance = "set it to one"
    g.execute("vehicle_hvac", action="set_fan", value=1)
    assert g.cabin.store.get(c.hvac_fan("Passenger")) == 1
    assert g.cabin.store.get(c.hvac_fan("Driver")) == 3


def test_ambient_red_is_a_real_write():
    jev = FakeJev(
        {"dev.lights": n(0.96)},
        {"lights.set_ambient_light": n(0.94), "lights.set_ambient_light.color": ch("red")},
    )
    g = OemToolGateway(enabled=["vehicle"], router="jev", jev_client=jev, jev_warm=False)
    out = g.take_cabin_turn("set the ambient light to red")
    assert g.cabin.store.get(c.AMBIENT_COLOR) == "red"
    assert out["speak"] == "Ambient light set to red."


def test_t9_a_side_jev_guessed_without_one_said_is_the_default():
    """"Okay, set the fan speed to one." changed only the driver's fan."""
    jev = FakeJev({"dev.hvac": n(0.95)}, {"hvac.set_fan": n(0.93), "hvac.set_fan.zone": ch("driver")})
    out = route_vehicle_jev("Okay, set the fan speed to one.", CabinEnv(), client=jev)
    assert out.calls[0].args["zone"] == "all"
    assert out.calls[0].args["value"] == 1


def test_a_side_the_driver_names_still_wins_over_jev():
    jev = FakeJev({"dev.hvac": n(0.95)}, {"hvac.set_fan": n(0.93), "hvac.set_fan.zone": ch("all")})
    out = route_vehicle_jev("set the passenger fan to one", CabinEnv(), client=jev)
    assert out.calls[0].args["zone"] == "passenger"
