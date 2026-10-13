# tests/test_cabingym_gate.py
import pytest

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.confirm import parse_confirmation
from nova_hailo.cabingym.gate import check
from nova_hailo.cabingym.types import VehicleCall


def snap(**init):
    store = c.build_store()
    if init:
        store.apply({c.resolve(k): v for k, v in init.items()})
    return store.snapshot()


def call(device, api, **args):
    return VehicleCall(device, api, args)


@pytest.mark.parametrize("vcall,init,verdict,reason", [
    (call("doors", "lock_doors", locked=False), {"speed": 60.0}, "refuse", "G1_unlock_moving"),
    (call("doors", "lock_doors", locked=False), {"speed": 3.0}, "execute", None),
    (call("doors", "lock_doors", locked=True), {"speed": 60.0}, "execute", None),
    (call("doors", "open_trunk"), {"speed": 40.0}, "refuse", "G1_trunk_moving"),
    (call("body", "set_sunroof", value=100), {"speed": 100.0}, "confirm_first", "G2_sunroof_speed"),
    (call("body", "set_sunroof", value=-100), {"speed": 100.0}, "confirm_first", "G2_sunroof_speed"),
    (call("body", "set_sunroof", value=0), {"speed": 100.0}, "execute", None),
    (call("body", "set_sunroof", value=100), {"speed": 50.0}, "execute", None),
    (call("phone", "send_message", recipient="Priya", text="hi"), {}, "confirm_first", "G3_message_readback"),
    (call("doors", "set_child_lock", on=False), {"speed": 30.0}, "confirm_first", "G5_child_lock_moving"),
    (call("doors", "set_child_lock", on=True), {"speed": 30.0}, "execute", None),
    (call("system", "set_drive_mode", mode="eco"), {"speed": 70.0, "gear": "D"}, "confirm_first", "G6_drive_mode_moving"),
    (call("system", "set_drive_mode", mode="eco"), {}, "execute", None),
    (call("phone", "call_contact", name="Mom"), {"phone.connected": False}, "refuse", "G8_no_phone"),
    (call("phone", "send_message", recipient="Mom", text="hi"), {"phone.connected": False}, "refuse", "G8_no_phone"),
])
def test_gate_rules(vcall, init, verdict, reason):
    v = check(vcall, snap(**init))
    assert (v.verdict, v.reason) == (verdict, reason)
    if verdict != "execute":
        assert v.speak


def test_g3_reads_back_the_message():
    v = check(call("phone", "send_message", recipient="Priya", text="on my way"), snap())
    assert "Priya" in v.speak and "on my way" in v.speak


def test_g4_truncates_reading_while_moving():
    v = check(call("phone", "read_messages"), snap(speed=50.0))
    assert v.verdict == "execute" and v.arg_overrides == {"_truncate": True}
    assert check(call("phone", "read_messages"), snap()).arg_overrides == {}


def test_g7_clamps_brightness_at_night():
    v = check(call("system", "set_display_brightness", value=5), snap(headlights="low"))
    assert v.verdict == "execute" and v.arg_overrides == {"value": 10}
    assert check(call("system", "set_display_brightness", value=5), snap()).arg_overrides == {}


@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("yes please", True), ("Yeah, do it", True), ("go ahead", True),
    ("send it", True), ("no", False), ("nope", False), ("cancel", False), ("never mind", False),
    ("stop", False), ("stop the music", None), ("open the sunroof", None), ("", None),
])
def test_parse_confirmation(text, expected):
    assert parse_confirmation(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("Switch it.", True), ("change it", True), ("do that", True), ("go for it", True),
    ("sounds good", True), ("yes please", True), ("absolutely", True), ("of course", True),
    ("Yes, change the comfort mode to eco mode.", True), ("Yeah, sure.", True),
    ("leave it", False), ("not now", False), ("keep it as it is", False),
    ("switch the radio to jazz", None),
])
def test_everyday_answers_to_a_confirmation(text, expected):
    assert parse_confirmation(text) is expected
