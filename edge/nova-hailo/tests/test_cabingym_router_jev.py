# tests/test_cabingym_router_jev.py
from nova_hailo.cabingym.grounding import NONE_OF_THESE
from nova_hailo.cabingym.router_jev import (
    PLACE_OTHER, route_vehicle_jev, route_with_jev_vehicle, stage1_questions, stage2_questions,
)
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.types import Intent


class FakeJev:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def system_one(self, state, questions, timeout=None):
        self.calls.append((state, questions))
        if not self.answers:
            raise TimeoutError("no answer")
        nxt = self.answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return {"answers": nxt}


def n(p):
    return {"noul": p}


def ch(v):
    return {"choice": v}


def test_stage1_has_one_question_per_device():
    cabin = CabinEnv()
    qs = stage1_questions(cabin)
    assert {k for k in qs if k.startswith("dev.")} == {f"dev.{d}" for d in cabin.devices}
    assert "smalltalk" in qs


def test_stage2_questions_cover_apis_enums_and_entities():
    qs = stage2_questions(["hvac", "phone"], CabinEnv())
    assert "hvac.set_temperature" in qs and "hvac.set_temperature.zone" in qs
    contacts = qs["phone.call_contact.name"]["criteria"]
    assert "Sam" in contacts and NONE_OF_THESE in contacts
    assert not any(k.startswith("media.") for k in qs)


def test_two_stage_temperature_and_call():
    jev = FakeJev(
        {"dev.hvac": n(0.94), "dev.phone": n(0.91), "dev.media": n(0.03)},
        {"hvac.set_temperature": n(0.92), "hvac.set_temperature.zone": ch("driver"),
         "phone.call_contact": n(0.95), "phone.call_contact.name": ch("Sam")},
    )
    out = route_vehicle_jev("make it a bit warmer for me and call Sam", CabinEnv(), client=jev)
    assert out.kind == "vehicle"
    assert [(x.device, x.api, x.args) for x in out.calls] == [
        ("hvac", "set_temperature", {"zone": "driver", "delta": 1.0}),
        ("phone", "call_contact", {"name": "Sam"}),
    ]
    assert out.calls[0].grounding["zone"] == "enum" and out.calls[1].grounding["name"] == "entity"
    assert out.trace["router"] == "jev" and set(out.trace["latency_ms"]) == {"stage1", "stage2"}
    assert out.trace["stage1"]["hvac"] == 0.94
    assert len(jev.calls) == 2


def test_unsure_stage1_returns_none_after_one_call():
    jev = FakeJev({"dev.hvac": n(0.3)})
    assert route_vehicle_jev("hmm", CabinEnv(), client=jev) is None
    assert len(jev.calls) == 1


def test_timeout_returns_none():
    assert route_vehicle_jev("call Sam", CabinEnv(), client=FakeJev(TimeoutError())) is None


def test_none_of_these_contact_clarifies():
    jev = FakeJev({"dev.phone": n(0.9)},
                  {"phone.call_contact": n(0.9), "phone.call_contact.name": ch(NONE_OF_THESE)})
    out = route_vehicle_jev("call Bob", CabinEnv(), client=jev)
    assert out.kind == "clarify"


def test_place_other_falls_back_to_text():
    jev = FakeJev({"dev.navigation": n(0.9)},
                  {"navigation.navigate_to": n(0.9), "navigation.navigate_to.destination": ch(PLACE_OTHER)})
    out = route_vehicle_jev("take me home", CabinEnv(), client=jev)
    assert out.calls[0].args["destination"]["name"] == "home"


def test_gateway_router_prefers_web_when_it_scores_higher():
    profile = CapabilityProfile.from_list(["web_search", "vehicle"])
    jev = FakeJev({"web_search": n(0.9), "dev.hvac": n(0.2)})
    routed = route_with_jev_vehicle("who won the match", profile, None, CabinEnv(), client=jev,
                                    timeout_sec=0.5, noul_min=0.65, max_actions=2)
    assert [r.intent for r in routed] == [Intent.WEB_SEARCH]
    assert len(jev.calls) == 1


def test_gateway_router_returns_vehicle_intent():
    profile = CapabilityProfile.from_list(["web_search", "vehicle"])
    jev = FakeJev({"web_search": n(0.1), "dev.windows": n(0.9)},
                  {"windows.set_window": n(0.9), "windows.set_window.zone": ch("driver")})
    (routed,) = route_with_jev_vehicle("open my window", profile, None, CabinEnv(), client=jev,
                                       timeout_sec=0.5, noul_min=0.65, max_actions=2)
    assert routed.intent is Intent.VEHICLE
    assert routed.slots["outcome"].calls[0].args == {"zone": "driver", "value": 100}
