"""Navigation calls corrected from what the driver said."""
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.nav import FixtureNav, Place
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.tools.oem_tools import OemToolGateway

THEATRE = Place("Landmark's Aquarius Theatre", 37.4448, -122.1622)
SUN_TEA = Place("Mr. Sun Tea Palo Alto", 37.4440, -122.1605)
LUCIE = Place("Lucie Stern Community Center", 37.4370, -122.1500)
HOPKINS = Place("Hopkins Avenue", 37.4380, -122.1440)
STANFORD = Place("Stanford Concert Practice Center", 37.4300, -122.1690)


class _Nav(FixtureNav):
    is_live = True

    def __init__(self):
        super().__init__((THEATRE, SUN_TEA, LUCIE, HOPKINS, STANFORD))


def _gateway(utterance, *, navigating_to=None):
    cabin = CabinEnv(live=True, nav=_Nav())
    g = OemToolGateway(enabled=["vehicle"], router="regex", cabin=cabin)
    if navigating_to is not None:
        g._cabin_utterance = ""
        assert g.execute("vehicle_navigation", action="navigate_to", destination=navigating_to)["status"] == "success"
    g._cabin_utterance = utterance
    return g, cabin


def _stops(cabin):
    return [s["name"] for s in cabin.store.get(c.NAV_STOPS)]


def test_stop_at_a_place_adds_a_stop_instead_of_ending_the_route():
    g, cabin = _gateway("Let's stop at the Stanford Concert Practice Center.", navigating_to="Landmark's Aquarius Theatre")
    out = g.execute("vehicle_navigation", action="stop_navigation")
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_ACTIVE) is True
    assert _stops(cabin) == ["Stanford Concert Practice Center"]


def test_stop_the_navigation_still_stops():
    g, cabin = _gateway("Stop the navigation.", navigating_to="Landmark's Aquarius Theatre")
    g.execute("vehicle_navigation", action="stop_navigation")
    assert cabin.store.get(c.NAV_ACTIVE) is False


def test_a_via_inside_the_destination_is_split_off():
    g, cabin = _gateway("Okay, let's navigate to the Lucie Stern Community Center via Hopkins Avenue.")
    out = g.execute("vehicle_navigation", action="navigate_to",
                    destination="Lucie Stern Community Center via Hopkins Avenue")
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Lucie Stern Community Center"
    assert _stops(cabin) == ["Hopkins Avenue"]


def test_first_this_then_that_keeps_the_final_destination():
    g, cabin = _gateway("First, let's go to the Mr Sun Tea then the theatre.", navigating_to="Landmark's Aquarius Theatre")
    out = g.execute("vehicle_navigation", action="navigate_to", destination="Mr. Sun Tea Palo Alto")
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Landmark's Aquarius Theatre"
    assert _stops(cabin) == ["Mr. Sun Tea Palo Alto"]


def test_theatres_are_a_place_kind_of_their_own():
    """ "best Dolby Atmos theaters nearby" searched coffee, then shopping malls."""
    from nova_hailo.cabingym.router_regex import route_vehicle_regex

    for said in ("find some theaters with Dolby Atmos nearby", "find a cinema nearby", "find movie theaters nearby"):
        out = route_vehicle_regex(said, CabinEnv())
        assert out is not None and out.calls[0].api == "find_nearby", said
        assert out.calls[0].args["category"] == "movie_theater", said


def test_an_empty_category_search_falls_back_to_text():
    from nova_hailo.cabingym.nav import MapboxNav

    seen = []

    def fetch(path, params):
        seen.append(path)
        if path.startswith("/category/"):
            return {"features": []}
        return {"features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.1622, 37.4448]},
                              "properties": {"name": "Landmark's Aquarius Theatre", "feature_type": "poi"}}]}

    hits = MapboxNav("pk.secret", fetch=fetch).search("movie theater", Place("here", 37.4419, -122.1430),
                                                      category="movie_theater")
    assert seen[0] == "/category/cinema"
    assert [h.name for h in hits] == ["Landmark's Aquarius Theatre"]


def test_the_claim_rescue_goes_to_a_listed_place_or_stops():
    g, cabin = _gateway("", navigating_to="Landmark's Aquarius Theatre")
    cabin.store.update({c.NAV_RESULTS: (Place("Blink Charging Station", 37.4430, -122.1600).to_dict(),)})
    out = g.rescue_claim("Go to Blink Charging Station.")
    assert out is not None and out["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Blink Charging Station"
    out = g.rescue_claim("Cancel the navigation no")
    assert out is not None and cabin.store.get(c.NAV_ACTIVE) is False
    assert g.rescue_claim("What's the weather like?") is None


def test_with_the_local_model_a_place_request_still_navigates():
    """A place request still navigates when the local model declines the tool."""
    from nova_hailo.edge_harness.types import Intent, RoutedIntent

    park = Place("El Camino Park", 37.4390, -122.1640)

    class _ParkNav(_Nav):
        def __init__(self):
            FixtureNav.__init__(self, (park, THEATRE))

    cabin = CabinEnv(live=True, nav=_ParkNav())
    g = OemToolGateway(enabled=["vehicle"], router="regex", cabin=cabin)
    g._jev_routes = lambda q: [RoutedIntent(Intent.CAPABILITY_UNAVAILABLE, q, {"speak": "I can't do that yet."})]
    out = g.route_and_execute("Hey, let's go to El Camino Park.")
    assert out is not None and out.get("status") == "success"
    assert cabin.store.get(c.NAV_DEST) == "El Camino Park"


def test_both_cloud_prompts_say_a_news_follow_up_searches_again():
    import inspect
    import sys
    import types
    from unittest.mock import MagicMock

    if "hailo_platform" not in sys.modules:   # pipeline imports it at load; not installed off the Pi
        hp, genai = types.ModuleType("hailo_platform"), types.ModuleType("hailo_platform.genai")
        hp.VDevice, genai.LLM, genai.Speech2Text, genai.Speech2TextTask = (MagicMock() for _ in range(4))
        hp.genai = genai
        sys.modules.update({"hailo_platform": hp, "hailo_platform.genai": genai})
    import nova_hailo.pipeline as pl

    src = inspect.getsource(pl.NovaPipeline)
    assert src.count("FOLLOW_UP_SEARCH_RULE") >= 2
    assert "Never reuse the previous topic" not in src
    assert "web_search" in pl.FOLLOW_UP_SEARCH_RULE


def test_a_place_right_by_the_car_is_not_zero_kilometres_away():
    from nova_hailo.cabingym.devices.navigation import spoken_distance

    assert spoken_distance(0.03) == "right by you"
    assert spoken_distance(0.34) == "about 350 metres away"
    assert spoken_distance(2.46) == "2.5 kilometres away"


# ---- V1c -------------------------------------------------------

MENLO = Place("Menlo Park", 37.4530, -122.1817)
MIDDLEFIELD = Place("Middlefield Road", 37.4460, -122.1550)
LAH = Place("Los Altos Hills", 37.3797, -122.1375)
BOFA = Place("Bank of America", 37.4440, -122.1610)
POPPY = Place("Poppy Bank", 37.4420, -122.1600, "bank")


def _v1c_gateway(utterance):
    class _V1cNav(_Nav):
        def __init__(self):
            FixtureNav.__init__(self, (MENLO, MIDDLEFIELD, LAH, BOFA, POPPY))

    cabin = CabinEnv(live=True, nav=_V1cNav())
    g = OemToolGateway(enabled=["vehicle"], router="regex", cabin=cabin)
    g._cabin_utterance = utterance
    return g, cabin


def test_a_via_given_twice_is_driven_through_once():
    g, cabin = _v1c_gateway("Let's go to the Menlo Park by Middlefield Road.")
    out = g.execute("vehicle_navigation", action="navigate_to",
                    destination="Menlo Park via Middlefield Road", via=["Middlefield Road"])
    assert out["status"] == "success"
    assert _stops(cabin) == ["Middlefield Road"]
    assert "Middlefield Road and Middlefield Road" not in out["speak"]


def test_a_highway_by_name_routes_the_fastest_way_and_says_so():
    g, cabin = _v1c_gateway("Let's go to the Los Altos Hills via the two hundred and eighty.")
    out = g.execute("vehicle_navigation", action="navigate_to", destination="Los Altos Hills", via=["US-280"])
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Los Altos Hills" and _stops(cabin) == []
    assert "US-280" in out["speak"] and "fastest" in out["speak"]


def test_go_to_a_named_brand_navigates_to_it_instead_of_listing_the_kind():
    g, cabin = _v1c_gateway("I want to go to the Bank of America now.")
    out = g.execute("vehicle_navigation", action="find_nearby", category="bank")
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Bank of America"
