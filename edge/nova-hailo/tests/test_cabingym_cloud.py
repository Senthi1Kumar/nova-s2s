# tests/test_cabingym_cloud.py
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.cloud_tools import cloud_call, vehicle_tool_schemas
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.edge_harness.openai_tools import openai_tools_for_profile, tool_call_kwargs
from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.tools.oem_tools import OemToolGateway


def test_schemas_one_tool_per_device():
    cabin = CabinEnv()
    tools = vehicle_tool_schemas(cabin.devices)
    names = [t["function"]["name"] for t in tools]
    assert names == [f"vehicle_{d}" for d in cabin.devices]
    hvac = next(t for t in tools if t["function"]["name"] == "vehicle_hvac")["function"]
    assert hvac["parameters"]["required"] == ["action"]
    assert "set_temperature" in hvac["parameters"]["properties"]["action"]["enum"]
    zone = hvac["parameters"]["properties"]["zone"]
    assert set(zone["enum"]) == {"driver", "passenger", "all"}
    for tool in tools:
        for spec in tool["function"]["parameters"]["properties"].values():
            assert not any(k.startswith("x-") for k in spec)


def test_cloud_call_grounds_and_validates():
    cabin = CabinEnv()
    out = cloud_call("vehicle_hvac", {"action": "set_temperature", "zone": "driver", "value": 21}, cabin)
    assert out.kind == "vehicle" and out.calls[0].args == {"zone": "driver", "value": 21.0}
    assert out.trace["router"] == "cloud"
    assert cloud_call("vehicle_hvac", {"action": "fly"}, cabin).kind == "clarify"
    assert cloud_call("vehicle_toaster", {"action": "x"}, cabin).kind == "decline"
    assert cloud_call("vehicle_seats", {"action": "set_seat_heating", "zone": "rear", "value": 2},
                      cabin).kind == "decline"
    assert cloud_call("vehicle_phone", {"action": "call_contact", "name": "Bob"}, cabin).kind == "clarify"


def test_cloud_find_nearby_declines_on_fixture_nav_in_live_mode():
    out = cloud_call("vehicle_navigation", {"action": "find_nearby", "category": "coffee"},
                     CabinEnv(live=True))
    assert out.kind == "decline"


def test_openai_tools_include_vehicle_only_when_enabled():
    cabin = CabinEnv()
    with_vehicle = openai_tools_for_profile(CapabilityProfile.from_list(["web_search", "vehicle"]),
                                            vehicle_devices=cabin.devices)
    names = [t["function"]["name"] for t in with_vehicle]
    assert "web_search" in names and "vehicle_hvac" in names and len(names) == 13
    without = openai_tools_for_profile(CapabilityProfile.from_list(["web_search"]),
                                       vehicle_devices=cabin.devices)
    assert [t["function"]["name"] for t in without] == ["web_search"]
    args = {"action": "set_ac", "on": False}
    assert tool_call_kwargs("vehicle_hvac", args) == args


def test_gateway_executes_cloud_vehicle_calls():
    g = OemToolGateway(enabled=["web_search", "vehicle"], router="regex")
    out = g.execute("vehicle_hvac", action="set_temperature", zone="driver", value=21.5)
    assert out["name"] == "vehicle" and out["status"] == "success"
    assert g.cabin.store.get(c.hvac_temp("Driver")) == 21.5
    assert out["result"]["trace"]["router"] == "cloud"


class _LiveNav:
    is_live = True

    def search(self, query, near, category=None, along_route=False):
        assert "palo alto club" in query.casefold()
        from nova_hailo.cabingym.nav import Place

        return [Place("Palo Alto Club", 37.445, -122.16)]

    def route(self, origin, stops):
        from nova_hailo.cabingym.nav import FixtureNav

        return FixtureNav().route(origin, stops)


def test_cloud_navigate_to_uses_a_live_place_search():
    cabin = CabinEnv(live=True, nav=_LiveNav())
    assert cabin.allow_poi_search is True
    g = OemToolGateway(enabled=["vehicle"], router="regex", cabin=cabin)
    out = g.execute("vehicle_navigation", action="navigate_to", destination="Palo Alto Club")
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_ACTIVE) is True
    assert cabin.store.get(c.NAV_DEST) == "Palo Alto Club"
    assert cabin.store.get(c.NAV_DEST_LAT) == 37.445


def test_rescue_runs_the_fan_when_the_model_called_navigation():
    g = OemToolGateway(enabled=["vehicle"], router="regex")
    missed = g.execute("vehicle_navigation", action="navigate_to")
    assert missed["status"] == "clarify"
    out = g.maybe_rescue_vehicle("set the fan speed to three", "vehicle_navigation", missed)
    assert out["status"] == "success"
    assert g.cabin.store.get(c.hvac_fan("Driver")) == 3
    assert out["result"]["trace"]["rescued_from"] == "vehicle_navigation"


def test_rescue_leaves_a_real_navigation_clarify_alone():
    g = OemToolGateway(enabled=["vehicle"], router="regex")
    missed = g.execute("vehicle_navigation", action="navigate_to", destination="the moon base")
    assert missed["status"] == "clarify"
    out = g.maybe_rescue_vehicle("navigate to the moon base", "vehicle_navigation", missed)
    assert out is missed
    assert g.cabin.store.get(c.NAV_ACTIVE) is False


def _point(name, lon, lat):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"name": name}}


def test_mapbox_nav_parses_search_box_forward_and_hides_the_token(caplog, monkeypatch):
    import logging

    from nova_hailo.cabingym.nav import MapboxNav, Place, mapbox_nav_from_env

    seen = []

    def fetch(path, params):
        seen.append((path, params.get("q"), "bbox" in params, params.get("auto_complete")))
        assert params["access_token"] == "pk.secret"
        assert params["proximity"].startswith("-122.14")
        return {"features": [_point("Palo Alto Club", -122.16, 37.445)]}

    here = Place("here", 37.44, -122.14)
    hits = MapboxNav("pk.secret", fetch=fetch).search("Palo Alto Club", here)
    assert [(p.name, p.lat, p.lon) for p in hits] == [("Palo Alto Club", 37.445, -122.16)]
    assert seen == [("/forward", "Palo Alto Club", True, "true")]

    def boom(path, params):
        raise RuntimeError("GET https://api.mapbox.com/search?access_token=pk.secret")

    with caplog.at_level(logging.INFO, logger="nova_hailo.cabingym.nav"):
        assert MapboxNav("pk.secret", fetch=boom).search("Palo Alto Club", here) == []
    assert "pk.secret" not in caplog.text
    monkeypatch.delenv("MAPBOX_API_KEY", raising=False)
    monkeypatch.delenv("MAPBOX_ACCESS_TOKEN", raising=False)
    assert mapbox_nav_from_env() is None
    monkeypatch.setenv("MAPBOX_API_KEY", "pk.present")
    live = mapbox_nav_from_env()
    assert live is not None and live.is_live is True


def test_place_name_scores_keep_a_local_partial_and_drop_a_far_weak_one():
    from nova_hailo.cabingym.nav import name_score

    assert name_score("Bowling Green Park", "Lawn Bowling Green") >= 0.68
    assert name_score("Bowling Green Park", "Sarah Wallis Park") < 0.68
    assert name_score("Lost Ghettos", "Lost Lane") < 0.68
    assert name_score("Barbaros Drive", "Barbados Drive") >= 0.68
    assert name_score("Palau Airport", "Palo Alto Airport of Santa Clara County") < 0.68
    assert name_score("Los Angeles", "Los Angeles") >= 0.84


def test_search_accepts_a_local_partial_name_before_a_far_exact_one():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    calls = []

    def fetch(path, params):
        calls.append(params.get("bbox"))
        south = float(params["bbox"].split(",")[1])
        # The tight box sits closer to the car than the wide one.
        if here.lat - south < 0.2:
            return {"features": [_point("Lawn Bowling Green", -122.14, 37.44)]}
        return {"features": [_point("Sarah Wallis Park", -122.13, 37.43)]}

    here = Place("here", 37.4419, -122.1430)
    hits = MapboxNav("pk.secret", fetch=fetch).search("Bowling Green Park", here)
    assert [p.name for p in hits] == ["Lawn Bowling Green"]
    # Wide name, tight name, then the shorter "Bowling Green" inside the tight box.
    assert len(calls) == 3


def test_search_keeps_a_clearly_named_far_city():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    def fetch(path, params):
        if "bbox" in params:
            return {"features": []}
        return {"features": [_point("Los Angeles", -118.24, 34.05)]}

    hits = MapboxNav("pk.secret", fetch=fetch).search(
        "Los Angeles", Place("here", 37.4419, -122.1430)
    )
    assert [p.name for p in hits] == ["Los Angeles"]


def test_nearby_airport_picks_the_closest_category_feature():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    def fetch(path, params):
        assert path == "/category/airport"
        assert "q" not in params
        return {"features": [
            _point("San Francisco International Airport", -122.39, 37.62),
            _point("Palo Alto Airport", -122.11, 37.455),
        ]}

    hits = MapboxNav("pk.secret", fetch=fetch).search(
        "the nearest airport", Place("here", 37.4419, -122.1430)
    )
    assert hits[0].name == "Palo Alto Airport"


def test_corrector_runs_once_after_a_miss_and_its_error_hides_the_token(caplog):
    import logging

    from nova_hailo.cabingym.nav import (
        MapboxNav, Place, clean_place_correction, hear_place_prompt,
    )

    queries = []

    def fetch(path, params):
        queries.append(params.get("q"))
        if params.get("q") == "Los Gatos":
            return {"features": [_point("Los Gatos", -121.98, 37.22)]}
        return {"features": [_point("Lost Lane", -88.0, 44.5)]}

    def correct(query, near):
        assert query == "Lost Ghettos"
        assert near.lat == 37.4419
        return '"Los Gatos"\nextra'

    here = Place("here", 37.4419, -122.1430)
    hits = MapboxNav("pk.secret", fetch=fetch, corrector=correct).search("Lost Ghettos", here)
    assert [p.name for p in hits] == ["Los Gatos"]
    assert queries.count("Lost Ghettos") == 3
    assert queries.count("Los Gatos") == 1

    def explode(query, near):
        raise RuntimeError("prompt leaked pk.secret")

    with caplog.at_level(logging.INFO, logger="nova_hailo.cabingym.nav"):
        assert MapboxNav("pk.secret", fetch=fetch, corrector=explode).search("Lost Ghettos", here) == []
    assert "pk.secret" not in caplog.text
    assert clean_place_correction("I'm not sure, maybe a park") is None
    prompt = hear_place_prompt(
        "Palo Alto", 37.4419, -122.1430, "Lost Ghettos",
        utterance="I would like to go to Lost Ghettos. Can you show me the direction?",
    )
    assert "37.4419" in prompt and "Lost Ghettos" in prompt
    assert "show me the direction" in prompt
    assert "Los Gatos" not in prompt and "Lawn Bowling" not in prompt


def test_a_shared_parameter_describes_each_actions_own_meaning():
    tools = {t["function"]["name"]: t["function"] for t in vehicle_tool_schemas(CabinEnv().devices)}
    body_value = tools["vehicle_body"]["parameters"]["properties"]["value"]["description"]
    assert "set_sunroof:" in body_value and "set_sunshade:" in body_value
    hvac_value = tools["vehicle_hvac"]["parameters"]["properties"]["value"]["description"]
    assert "set_temperature:" in hvac_value and "set_fan:" in hvac_value


def test_the_corrector_may_offer_several_names_and_the_first_real_one_nearby_wins():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    def fetch(path, params):
        if params.get("q") == "Bullis Charter School":
            return {"features": [_point("Bullis Charter School", -122.12, 37.38)]}
        return {"features": []}            # "Burlingame" (a far guess) and the heard name miss

    seen = {}

    def correct(query, near):
        seen["query"] = query
        return "Burlingame Charter School\nBullis Charter School\nBullis-Purissima"

    hits = MapboxNav("pk.x", fetch=fetch, corrector=correct).search(
        "Bully's Charter School", Place("here", 37.38, -122.14))
    assert [p.name for p in hits] == ["Bullis Charter School"]


def test_a_near_tie_goes_to_the_nearest_place_unless_the_name_says_where():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    def fetch(path, params):
        return {"features": [
            _point("Alamo Drafthouse Cinema New Mission", -122.419, 37.752),    # 50 km
            _point("Alamo Drafthouse Cinema Mountain View", -122.078, 37.393),  # 6 km
        ]}

    here = Place("here", 37.4419, -122.1430)
    nav = MapboxNav("pk.x", fetch=fetch)
    assert nav.search("the alamo draftos cinema", here)[0].name.endswith("Mountain View")
    assert nav.search("alamo drafthouse new mission", here)[0].name.endswith("New Mission")


def test_the_place_corrector_knows_where_the_car_is_heading():
    from nova_hailo.cabingym.nav import hear_place_prompt

    prompt = hear_place_prompt("Los Altos Hills", 37.38, -122.14, "Bully's Charter School",
                               destination="Los Altos")
    assert "driving to Los Altos" in prompt and "one per line" in prompt
    assert "street address" in prompt


def test_bowling_green_park_near_palo_alto_is_the_lawn_not_the_san_jose_street():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    def fetch(path, params):
        query = (params.get("q") or "").casefold()
        bbox = params.get("bbox")
        tight = False
        if bbox:
            south, north = (float(x) for x in bbox.split(",")[1::2])
            tight = (north - south) < 0.3
        if query == "bowling green" and tight:
            return {"features": [_point("Lawn Bowling Green", -122.143, 37.445)]}
        if query == "bowling green park" and not tight:
            return {"features": [_point("Bowling Green Drive", -121.85, 37.25)]}
        return {"features": []}

    here = Place("here", 37.4419, -122.1430)
    hits = MapboxNav("pk.x", fetch=fetch).search("Bowling Green Park, Palo Alto", here)
    assert [p.name for p in hits] == ["Lawn Bowling Green"]


def test_a_named_place_missing_from_the_index_uses_its_nearby_street_address():
    from nova_hailo.cabingym.nav import MapboxNav, Place, haversine_km

    def fetch(path, params):
        query = params.get("q") or ""
        if query.startswith("1313"):
            return {"features": [_point("1313 Newell Road", -122.13900, 37.44398)]}
        return {"features": [_point(
            "Palo Alto High School Performing Arts Center", -122.151, 37.449)]}

    def correct(query, near):
        assert query == "Palo Alto Art Center"
        return "1313 Newell Road, Palo Alto"

    here = Place("here", 37.4419, -122.1430)
    hits = MapboxNav("pk.x", fetch=fetch, corrector=correct).search("Palo Alto Art Center", here)
    assert [p.name for p in hits] == ["Palo Alto Art Center"]
    assert haversine_km(here, hits[0]) < 1.0


def test_a_name_and_address_on_one_line_still_pins_the_nearby_building():
    from nova_hailo.cabingym.nav import MapboxNav, Place, haversine_km

    def fetch(path, params):
        if str(params.get("q") or "").startswith("1313"):
            return {"features": [_point("1313 Newell Road", -122.13900, 37.44398)]}
        return {"features": []}

    def correct(query, near):
        return "Palo Alto Art Center, 1313 Newell Rd, Palo Alto, CA"

    here = Place("here", 37.4419, -122.1430)
    hits = MapboxNav("pk.x", fetch=fetch, corrector=correct).search("Palo Alto Art Center", here)
    assert [p.name for p in hits] == ["Palo Alto Art Center"]
    assert haversine_km(here, hits[0]) < 1.0


def test_the_art_center_does_not_become_the_high_school():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    def fetch(path, params):
        return {"features": [_point(
            "Palo Alto High School Performing Arts Center", -122.151, 37.449)]}

    def correct(query, near):
        return "Palo Alto High School Performing Arts Center"

    here = Place("here", 37.4419, -122.1430)
    assert MapboxNav("pk.x", fetch=fetch, corrector=correct).search(
        "Palo Alto Art Center", here) == []


def test_a_supplied_place_that_search_misses_is_not_an_empty_destination():
    from nova_hailo.cabingym.nav import FixtureNav

    class _Miss(FixtureNav):
        is_live = True

        def search(self, *args, **kwargs):
            return []

    cabin = CabinEnv(live=True, nav=_Miss())
    missed = cloud_call(
        "vehicle_navigation",
        {"action": "navigate_to", "destination": "Palo Alto Art Center"},
        cabin,
    )
    assert missed.kind == "clarify"
    assert missed.speak == "I couldn't find Palo Alto Art Center."
    empty = cloud_call("vehicle_navigation", {"action": "navigate_to"}, cabin)
    assert empty.speak == "Where would you like to go?"


def test_the_exact_landmark_beats_a_nearer_shop_named_after_it():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    def fetch(path, params):
        return {"features": [_point("Golden Gate Bridge Bike Rentals", -122.42, 37.80),
                             _point("Golden Gate Bridge", -122.4783, 37.8199)]}

    here = Place("here", 37.79, -122.40)
    hits = MapboxNav("pk.secret", fetch=fetch).search("the Golden Gate Bridge", here)
    assert [p.name for p in hits] == ["Golden Gate Bridge"]


def test_a_name_with_its_town_is_scored_by_the_name():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    queries = []

    def fetch(path, params):
        queries.append(params["q"])
        return {"features": [_point("Dar", -121.98, 37.226), _point("Los Gatos", -121.97, 37.23)]}

    hits = MapboxNav("pk.secret", fetch=fetch).search("Dar, Los Gatos", Place("here", 37.4419, -122.1430))
    assert [p.name for p in hits] == ["Dar"]
    assert queries[0] == "Dar, Los Gatos"     # searched whole, so the map knows which Dar
