# tests/test_cabingym_slots.py
import pytest

from nova_hailo.cabingym import slots as s


@pytest.mark.parametrize("text,expected", [
    ("set it to 21", 21.0), ("twenty one and a half", 21.5), ("21.5 degrees", 21.5),
    ("a hundred percent", 100.0), ("sixty", 60.0), ("turn on the ac", None), ("someone", None),
])
def test_parse_number(text, expected):
    assert s.parse_number(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("set the temperature to 23 degrees", {"value": 23.0}),
    ("set it to 72 degrees", {"value": 22.0}),
    ("set the temperature to 35", {"value": 35.0}),
    ("I'm a bit cold", {"delta": 1.0}),
    ("make it warmer", {"delta": 1.0}),
    ("make it a lot cooler", {"delta": -2.0}),
    ("it's too warm in here", {"delta": -1.0}),
    ("temperature please", None),
])
def test_parse_temperature(text, expected):
    assert s.parse_temperature(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("set the volume to 25", {"value": 25}), ("turn the volume up", {"delta": 10}),
    ("turn it down", {"delta": -10}), ("volume to max", {"value": 100}),
    ("mute the music", {"value": 0}), ("dim the screen", {"delta": -10}),
    ("volume", None),
])
def test_parse_percent(text, expected):
    assert s.parse_percent(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("open my window", {"value": 100}), ("close the sunroof", {"value": 0}),
    ("crack the rear windows", {"value": 10}), ("put the window down halfway", {"value": 50}),
    ("tilt the sunroof", {"value": -100}), ("open it 30 percent", {"value": 30}),
    ("the window", None),
])
def test_parse_opening(text, expected):
    assert s.parse_opening(text) == expected


def test_parse_shade():
    assert s.parse_shade("raise the rear shade") == {"value": 100}
    assert s.parse_shade("close the sunshade") == {"value": 100}
    assert s.parse_shade("open the sunshade") == {"value": 0}
    assert s.parse_shade("the shade") is None


@pytest.mark.parametrize("text,expected", [
    ("turn off the seat heating", {"value": 0}), ("seat heating on high", {"value": 3}),
    ("turn the fan up", {"delta": 1}), ("fan down a bit", {"delta": -1}),
    ("turn on my seat heater", {"value": 2}), ("fan to 4", {"value": 4}), ("fan to max", {"value": 6}),
])
def test_parse_level(text, expected):
    max_level, on_level = (6, 3) if "fan" in text else (3, 2)
    assert s.parse_level(text, max_level, on_level) == expected


def test_bool_parsers():
    assert s.parse_on_off("turn off the AC") is False
    assert s.parse_on_off("turn on the defroster") is True
    assert s.parse_on_off("defrost the rear window") is None
    assert s.parse_lock("unlock the car") is False
    assert s.parse_lock("lock the doors") is True
    assert s.parse_fold("fold the mirrors") is True
    assert s.parse_fold("unfold the mirrors") is False
    assert s.parse_recirc("switch to fresh air") is False
    assert s.parse_recirc("recirculate the air") is None
    assert s.parse_mute("unmute the call") is False
    assert s.parse_mute("mute me") is True


HVAC = ("driver", "passenger", "all")


@pytest.mark.parametrize("text,allowed,default,expected", [
    ("set the passenger temperature to 19", HVAC, "driver", ("passenger", "parser")),
    ("I'm a bit cold", HVAC, "driver", ("driver", "parser")),
    ("set driver and passenger to 20 degrees", HVAC, "driver", ("all", "parser")),
    ("set the temperature to 23", HVAC, "driver", ("driver", "default")),
    ("heat the rear seats", HVAC, "driver", (None, "unsupported")),
    ("crack the rear windows", ("driver", "passenger", "rear_left", "rear_right", "front", "rear", "all"),
     "driver", ("rear", "parser")),
    ("close the windows", ("driver", "passenger", "rear", "all"), "driver", ("all", "parser")),
    ("reading lights in the back", ("front", "rear", "all"), "front", ("rear", "parser")),
    ("turn it up", ("front", "rear"), None, (None, "missing")),
])
def test_parse_zone(text, allowed, default, expected):
    assert s.parse_zone(text, allowed, default) == expected


def test_parse_enum_synonyms_and_bool_map():
    allowed = ("off", "auto", "low", "high")
    syn = {"high": ("high beams", "brights"), "low": ("low beams", "dipped")}
    assert s.parse_enum("high beams on", allowed, syn) == "high"
    assert s.parse_enum("switch the headlights off", allowed, syn) == "off"
    assert s.parse_enum("turn the wipers on", allowed, {}, {"true": "low", "false": "off"}) == "low"
    assert s.parse_enum("wipers please", allowed, {}) is None


def test_spans():
    assert s.extract_message("text Priya that I'm on my way.") == "I'm on my way"
    assert s.extract_message("tell Sam I'll be late") == "I'll be late"
    assert s.extract_message("text Priya") is None
    assert s.extract_media_query("play some jazz") == "jazz"
    assert s.extract_media_query("play Midnight City on spotify") == "Midnight City"
    assert s.extract_media_query("pause") is None


def test_entities():
    names = ["Sam", "Priya", "Alex", "Mom"]
    assert s.match_entity("sam", names) == "Sam"
    assert s.match_entity("pri", names) == "Priya"
    assert s.match_entity("Bob", names) is None
    assert s.find_entity_in_text("navigate to Sam's place", names) == "Sam"
    assert s.find_entity_in_text("call Bob", names) is None


@pytest.mark.parametrize("text,expected", [
    ("make it a bit warmer for me and turn the fan up",
     ["make it a bit warmer for me", "turn the fan up"]),
    ("make it warmer for me and call Sam", ["make it warmer for me", "call Sam"]),
    ("set driver and passenger to 20 degrees", ["set driver and passenger to 20 degrees"]),
    ("set the temperature to 21 and the volume to 30",
     ["set the temperature to 21", "the volume to 30"]),
    ("navigate home and text Sam that I'll grab milk and bread",
     ["navigate home", "text Sam that I'll grab milk and bread"]),
    ("", []),
])
def test_split_clauses(text, expected):
    assert s.split_clauses(text) == expected
