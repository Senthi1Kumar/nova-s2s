# tests/test_cabingym_catalog.py
import pytest

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.adapters.sim import InProcessSim


def test_paths_unique_and_vss_shaped():
    paths = [s.path for s in c.SIGNALS]
    assert len(paths) == len(set(paths))
    assert all(p.startswith("Vehicle.") for p in paths)


def test_every_alias_resolves_to_a_signal():
    paths = {s.path for s in c.SIGNALS}
    for alias, path in c.ALIASES.items():
        assert path in paths, alias


def test_resolve_accepts_alias_and_full_path():
    assert c.resolve("hvac.driver.temp") == c.hvac_temp("Driver")
    assert c.resolve(c.SPEED) == c.SPEED
    with pytest.raises(KeyError):
        c.resolve("no.such.alias")


def test_ev_profile_has_soc_not_fuel():
    store = c.build_store("ev_demo")
    assert store.has(c.SOC) and not store.has(c.FUEL)
    assert store.get(c.RANGE) == 210.0


def test_ice_profile_has_fuel_not_soc():
    store = c.build_store("audi_q7_ice")
    assert store.has(c.FUEL) and store.has(c.RPM) and not store.has(c.SOC)
    assert store.get(c.RANGE) == 520.0


def test_unknown_profile():
    with pytest.raises(ValueError):
        c.get_profile("tesla")


def test_defaults_are_demo_ready():
    s = c.build_store()
    assert s.get(c.hvac_temp("Driver")) == 21.0
    assert s.get(c.PHONE_CONNECTED) is True
    assert [x["name"] for x in s.get(c.CONTACTS)] == ["Sam", "Priya", "Alex", "Mom"]
    assert {m["id"] for m in s.get(c.MESSAGES)} == {"m1", "m2"}
    assert [p["name"] for p in s.get(c.NAV_SAVED)] == ["home", "work"]


def test_derived_signals_unscored():
    store = c.build_store()
    scored = set(c.scored_paths(store))
    for path in (c.NAV_ETA, c.NAV_DIST, c.NAV_DEST_LAT, c.NAV_DEST_LON, c.NAV_RESULTS):
        assert path not in scored
    assert c.hvac_temp("Driver") in scored


def test_sim_immediate():
    store = c.build_store()
    InProcessSim().attach(store)
    store.set_targets({c.SUNROOF: 100})
    assert store.get(c.SUNROOF) == 100


def test_sim_ramp_moves_then_settles():
    store = c.build_store()
    sim = InProcessSim(ramp=True)
    sim.attach(store)
    store.set_targets({c.hvac_temp("Driver"): 24.0, c.AC: False})
    assert store.get(c.hvac_temp("Driver")) == 21.0
    assert store.get(c.AC) is False
    sim.tick(1.0)
    assert store.get(c.hvac_temp("Driver")) == 21.5
    sim.settle()
    assert store.get(c.hvac_temp("Driver")) == 24.0


def test_sim_ramp_int_progresses():
    store = c.build_store()
    sim = InProcessSim(ramp=True)
    sim.attach(store)
    store.set_targets({c.SUNROOF: 100})
    sim.tick(0.01)
    assert store.get(c.SUNROOF) == 1
