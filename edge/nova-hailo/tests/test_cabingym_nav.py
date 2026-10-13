# tests/test_cabingym_nav.py
import pytest

from nova_hailo.cabingym.nav import FixtureNav, Place, haversine_km

HERE = Place("here", 37.4419, -122.1430)


def test_haversine_known_distance():
    assert haversine_km(HERE, Place("home", 37.4275, -122.1697)) == pytest.approx(2.85, abs=0.05)


def test_search_by_category_sorted_by_distance():
    res = FixtureNav().search("coffee", HERE, category="coffee")
    assert [p.name for p in res] == ["Hilltop Cafe", "Blue Door Coffee"]


def test_search_by_query_words():
    assert FixtureNav().search("nearest coffee shop", HERE)[0].name == "Hilltop Cafe"


def test_search_unknown_returns_empty():
    assert FixtureNav().search("moon base", HERE) == []


def test_route_is_deterministic():
    nav = FixtureNav()
    home = Place("home", 37.4275, -122.1697)
    r1, r2 = nav.route(HERE, [home]), nav.route(HERE, [home])
    assert r1 == r2
    assert r1.distance_km == pytest.approx(3.7, abs=0.1)
    assert r1.eta_min == pytest.approx(5.6, abs=0.2)


def test_place_round_trip():
    p = Place("X", 1.0, 2.0, "coffee")
    assert Place.from_dict(p.to_dict()) == p
    assert FixtureNav.is_live is False


def test_the_place_corrector_is_told_the_heard_spelling_found_nothing_nearby():
    from nova_hailo.cabingym.nav import hear_place_prompt

    prompt = hear_place_prompt("Los Altos Hills", 37.38, -122.14, "Bully's Charter School",
                               utterance="add a stop at Bully's Charter School")
    assert "no place" in prompt.lower() and "sound" in prompt.lower()


# ---- Along the route, within a distance or a drive time ------------------------

from nova_hailo.cabingym import catalog as c  # noqa: E402
from nova_hailo.cabingym.executor import VehicleExecutor  # noqa: E402
from nova_hailo.cabingym.grounding import resolve_place  # noqa: E402
from nova_hailo.cabingym.router_regex import route_vehicle_regex  # noqa: E402
from nova_hailo.cabingym.runtime import CabinEnv, route_ahead  # noqa: E402
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall  # noqa: E402

# Palo Alto: from the car south-west along to Page Mill, passing Page Mill Fuel.
ROAD = [(37.4419, -122.1430), (37.4300, -122.1380), (37.4210, -122.1335), (37.4100, -122.1300)]


def _find(cabin, **args):
    call = VehicleCall("navigation", "find_nearby", {"category": "fuel", **args})
    return VehicleExecutor(cabin).run(RouteOutcome("vehicle", [call], "", {}))


def _cabin():
    return CabinEnv()   # not live: fixture places may be searched


def test_the_road_ahead_starts_at_the_car():
    ahead = route_ahead(ROAD, 37.4300, -122.1380)
    assert ahead[0] == ROAD[1] and ahead[-1] == ROAD[-1]
    assert route_ahead(ROAD, 37.41, -122.13) == ()            # at the end: nothing ahead
    assert len(route_ahead([(0.0, i / 100) for i in range(500)], 0.0, 0.0, max_points=60)) == 60


def test_along_the_route_keeps_only_places_by_the_road():
    cabin = _cabin()
    cabin.route_points = list(ROAD)
    out = _find(cabin, along_route=True)
    assert "on your route" in out["speak"]
    names = [p["name"] for p in cabin.store.get(c.NAV_RESULTS)]
    assert names and all(n in {"El Camino Fuel", "Page Mill Fuel"} for n in names)


def test_along_the_route_without_a_route_asks_first():
    out = _find(_cabin(), along_route=True)
    assert out["status"] == "clarify" and "not on a route" in out["speak"]


def test_within_minutes_drops_places_too_far_to_drive():
    cabin = _cabin()
    out = _find(cabin, within_min=1)
    assert "couldn't find" in out["speak"] and "within 1 minutes" in out["speak"]
    out = _find(cabin, within_min=15)
    assert "minutes away" in out["speak"] or "minute away" in out["speak"]


def test_the_second_one_is_the_second_search_result():
    cabin = _cabin()
    cabin.store.set_targets({c.NAV_RESULTS: ({"name": "Don Giovanni", "lat": 37.39, "lon": -122.08},
                                             {"name": "Dar", "lat": 37.22, "lon": -121.98})})
    place, how = resolve_place("add the second one as a stop", cabin)
    assert place["name"] == "Dar" and how == "entity"


def test_the_router_hears_on_the_way_and_a_time_limit():
    cabin = _cabin()
    out = route_vehicle_regex("find a gas station on the way", cabin)
    assert out.calls[0].api == "find_nearby" and out.calls[0].args["along_route"] is True
    out = route_vehicle_regex("find parking within 10 minutes", cabin)
    assert out.calls[0].args["within_min"] == 10


def test_mapbox_searches_along_the_route_and_times_the_drive():
    from nova_hailo.cabingym.nav import MapboxNav, Place

    seen = []

    def fetch(path, params):
        seen.append((path, params))
        if path.startswith("/directions-matrix/"):
            return {"durations": [[0, 240.0, None]]}
        return {"features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.134, 37.421]},
             "properties": {"name": "Page Mill Fuel"}}]}

    nav = MapboxNav("pk.secret", fetch=fetch)
    here = Place("here", 37.4419, -122.1430)
    hits = nav.search("fuel", here, category="fuel", route=tuple(ROAD))
    path, params = seen[0]
    assert path == "/category/gas_station" and params["sar_type"] == "isochrone"
    assert params["route_geometry"] == "polyline6" and params["time_deviation"] == 10 and params["route"]
    assert nav.drive_minutes(here, [hits[0], Place("x", 0, 0)]) == [4.0, None]
    assert seen[-1][0].startswith("/directions-matrix/v1/mapbox/driving-traffic/-122.143000,37.441900;")
