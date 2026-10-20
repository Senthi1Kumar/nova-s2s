"""Routes through named places ("... via Los Altos, Cupertino and Campbell") and a street
named with its town ("Barbara Drive in Los Gatos").

"The Barbara Drive in Los Gatos via Los Altos, Cupertino, Campbell" went to the
Barbara Drive in Palo Alto on the expressway: navigate_to took one destination, and the
model dropped the town.
"""
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.nav import FixtureNav, Place
from nova_hailo.cabingym.router_regex import route_vehicle_regex
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.tools.oem_tools import OemToolGateway

TOWNS = (
    Place("Los Altos", 37.3852, -122.1141),
    Place("Cupertino", 37.3230, -122.0322),
    Place("Campbell", 37.2872, -121.9500),
    Place("Barbara Drive, Los Gatos", 37.2266, -121.9747),
    Place("Barbara Drive, Palo Alto", 37.4370, -122.1290),
)
VIA = ["Los Altos", "Cupertino", "Campbell"]


class _Nav(FixtureNav):
    is_live = True

    def __init__(self):
        super().__init__(TOWNS)
        self.queries: list[str] = []

    def search(self, query, near, *args, **kw):
        self.queries.append(query)
        return super().search(query, near, *args, **kw)


def _gateway():
    cabin = CabinEnv(live=True, nav=_Nav())
    return OemToolGateway(enabled=["vehicle"], router="regex", cabin=cabin), cabin


def _stops(cabin):
    return [s["name"] for s in cabin.store.get(c.NAV_STOPS)]


def test_a_cloud_route_passes_through_the_places_in_order():
    g, cabin = _gateway()
    out = g.execute("vehicle_navigation", action="navigate_to",
                    destination="Barbara Drive, Los Gatos", via=VIA)
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Barbara Drive, Los Gatos"
    assert _stops(cabin) == VIA
    assert "via Los Altos, Cupertino and Campbell" in out["speak"]


def test_via_given_as_one_sentence_is_split():
    g, cabin = _gateway()
    out = g.execute("vehicle_navigation", action="navigate_to",
                    destination="Barbara Drive, Los Gatos", via="Los Altos, Cupertino and Campbell")
    assert out["status"] == "success"
    assert _stops(cabin) == VIA


def test_a_via_place_that_cannot_be_found_asks_instead_of_dropping_it():
    g, cabin = _gateway()
    out = g.execute("vehicle_navigation", action="navigate_to",
                    destination="Barbara Drive, Los Gatos", via=["Los Altos", "Narnia"])
    assert out["status"] == "clarify"
    assert "Narnia" in out["speak"]
    assert cabin.store.get(c.NAV_ACTIVE) is False


def test_a_street_in_a_town_is_searched_with_its_town():
    g, cabin = _gateway()
    out = g.execute("vehicle_navigation", action="navigate_to", destination="Barbara Drive in Los Gatos")
    assert out["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Barbara Drive, Los Gatos"
    assert "Barbara Drive, Los Gatos" in cabin.nav.queries


def test_a_spoken_via_route_through_the_fast_router():
    cabin = CabinEnv(live=True, nav=_Nav())
    out = route_vehicle_regex(
        "I would like to go to the Barbara Drive in Los Gatos via Los Altos, Cupertino and Campbell.", cabin)
    assert out is not None and out.kind == "vehicle"
    call = out.calls[0]
    assert call.api == "navigate_to"
    assert call.args["destination"]["name"] == "Barbara Drive, Los Gatos"
    assert [v["name"] for v in call.args["via"]] == VIA
