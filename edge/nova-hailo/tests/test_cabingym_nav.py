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
