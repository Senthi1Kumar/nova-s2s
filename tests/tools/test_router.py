"""Tests for the lexical (weighted token-overlap) tool router.

No mocking: builds a real registry via ``build_registry`` and scores/ranks
it against realistic driver utterances.
"""
from __future__ import annotations

import tempfile

from nova.server.tool_service import build_registry
from nova.tools.router import ToolRouter
from nova.tools.vehicle import VehicleDB


def _real_registry():
    return build_registry(
        VehicleDB(tempfile.mktemp()),
        driveauth_store=tempfile.mkdtemp(),
    )


def test_hvac_query_ranks_set_hvac_top():
    router = ToolRouter(_real_registry())
    top = router.top_k("set the driver zone temperature to 22 degrees", k=3)
    assert top[0]["name"] == "set_hvac"


def test_payment_query_ranks_send_payment_top():
    router = ToolRouter(_real_registry())
    top = router.top_k("pay fifty rupees to chai point", k=3)
    assert top[0]["name"] == "send_payment"


def test_weather_query_ranks_get_weather_top():
    router = ToolRouter(_real_registry())
    top = router.top_k("what's the weather like in truchi tamil nadu", k=3)
    assert top[0]["name"] == "get_weather"


def test_outdoor_temperature_ranks_get_weather_over_hvac():
    router = ToolRouter(_real_registry())
    top = router.top_k("what's the temperature outside", k=3)
    assert top[0]["name"] == "get_weather"


def test_bangalore_weather_ranks_get_weather_top():
    router = ToolRouter(_real_registry())
    top = router.top_k("current temperature weather in Bangalore", k=3)
    assert top[0]["name"] == "get_weather"


def test_email_query_ranks_check_email_top():
    router = ToolRouter(_real_registry())
    top = router.top_k("Check my emails.", k=3)
    assert top[0]["name"] == "check_email"


def test_cabin_temperature_ranks_vehicle_status_over_email():
    router = ToolRouter(_real_registry())
    top = router.top_k("Hey, can you check my cabin temperature.", k=3)
    assert top[0]["name"] == "query_vehicle_status"


def test_tell_me_zones_ranks_vehicle_status_over_hvac():
    router = ToolRouter(_real_registry())
    top = router.top_k("Now tell me all the three zones, cabin temperature.", k=3)
    assert top[0]["name"] == "query_vehicle_status"


def test_stock_query_ranks_web_search_top():
    router = ToolRouter(_real_registry())
    top = router.top_k("What is the current stock price of Nvidia", k=3)
    assert top[0]["name"] == "web_search"


def test_zero_score_chitchat_does_not_prefer_unrelated_tools_first():
    """With no lexical signal, pinned tools still win; otherwise any stable
    ranking is fine — just don't crash and keep schema shape."""
    router = ToolRouter(_real_registry())
    top = router.top_k("Yeah.", k=3, pinned={"send_payment"})
    assert top[0]["name"] == "send_payment"
    assert len(top) == 3


def test_top_k_returns_requested_count_and_well_formed_schemas():
    router = ToolRouter(_real_registry())
    top = router.top_k("pay fifty rupees to chai point", k=5)
    assert len(top) == 5
    for ft in top:
        assert ft["type"] == "function"
        assert "name" in ft and "description" in ft and "parameters" in ft


def test_top_k_is_deterministic():
    router = ToolRouter(_real_registry())
    first = router.top_k("open the sunroof", k=4)
    second = router.top_k("open the sunroof", k=4)
    assert [t["name"] for t in first] == [t["name"] for t in second]


def test_name_match_outweighs_description_only_match():
    """A query containing a tool's exact name should beat a tool whose
    description merely happens to share a common word."""
    router = ToolRouter(_real_registry())
    top = router.top_k("recall_memories please", k=1)
    assert top[0]["name"] == "recall_memories"


def test_synonym_heat_routes_to_hvac():
    r = ToolRouter(_real_registry())
    assert r.top_k("turn up the heat please", k=3)[0]["name"] == "set_hvac"


def test_roll_down_routes_to_windows():
    r = ToolRouter(_real_registry())
    assert r.top_k("roll down the driver window", k=3)[0]["name"] == "set_windows"


def test_pinned_tools_always_included():
    r = ToolRouter(_real_registry())
    top = r.top_k("what's the weather", k=3, pinned={"send_payment"})
    assert "send_payment" in [t["name"] for t in top]


def test_stock_price_amazon_ranks_web_search_not_vehicle():
    router = ToolRouter(_real_registry())
    top = router.top_k("Okay, so now tell me the current stock price of Amazon.", k=3)
    assert top[0]["name"] == "web_search"


def test_reminders_query_ranks_list_reminders_not_vehicle():
    router = ToolRouter(_real_registry())
    top = router.top_k("Now, tell me do you have any reminders for now.", k=3)
    assert top[0]["name"] == "list_reminders"


def test_play_jazz_ranks_play_music():
    router = ToolRouter(_real_registry())
    top = router.top_k("Hey, can you play some jazz.", k=3)
    assert top[0]["name"] == "play_music"
