"""A close match in the last search wins over a live search, and a place
hundreds of kilometres away is confirmed before the route changes.
"""
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym import gate
from nova_hailo.cabingym.nav import FixtureNav, Place
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.types import VehicleCall
from nova_hailo.tools.oem_tools import OemToolGateway

KIRKS = Place("Kirk's Steakburgers", 37.4300, -122.1220)
SUN_TEA = Place("Mr. Sun Tea Mountain View", 37.3940, -122.0800)
FAR = Place("Mr. Santi", 40.7128, -74.0060)   # New York, about 4,100 km from Palo Alto


class _Nav(FixtureNav):
    is_live = True

    def __init__(self):
        super().__init__((KIRKS, SUN_TEA, FAR))


def _navigating():
    cabin = CabinEnv(live=True, nav=_Nav())
    g = OemToolGateway(enabled=["vehicle"], router="regex", cabin=cabin)
    assert g.execute("vehicle_navigation", action="navigate_to", destination="Kirk's Steakburgers")["status"] == "success"
    cabin.store.update({c.NAV_RESULTS: tuple(p.to_dict() for p in (SUN_TEA, Place("Starbucks", 37.39, -122.07),
                                                                    Place("1 Oz Coffee", 37.38, -122.06)))})
    return g, cabin


def test_a_misheard_name_matches_the_last_search_before_searching_the_world():
    g, cabin = _navigating()
    out = g.execute("vehicle_navigation", action="add_stop", destination="Mr Santi")
    assert out["status"] == "success"
    assert [s["name"] for s in cabin.store.get(c.NAV_STOPS)] == ["Mr. Sun Tea Mountain View"]


def test_a_place_hundreds_of_kilometres_away_is_confirmed_first():
    cabin = CabinEnv(live=True, nav=_Nav())
    g = OemToolGateway(enabled=["vehicle"], router="regex", cabin=cabin)
    out = g.execute("vehicle_navigation", action="navigate_to", destination="Mr Santi")
    assert out["status"] == "confirm"
    assert "km away" in out["speak"] and "Mr. Santi" in out["speak"]
    assert cabin.store.get(c.NAV_ACTIVE) is False
    yes = g.vehicle.resolve_confirm(True)
    assert yes["status"] == "success"
    assert cabin.store.get(c.NAV_DEST) == "Mr. Santi"


def test_a_nearby_place_goes_straight_through_the_gate():
    snap = CabinEnv().store.snapshot()
    call = VehicleCall("navigation", "navigate_to", {"destination": KIRKS.to_dict()}, {})
    assert gate.check(call, snap).verdict == "execute"
    far = VehicleCall("navigation", "add_stop", {"destination": FAR.to_dict()}, {})
    assert gate.check(far, snap).verdict == "confirm_first"
