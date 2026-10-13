"""The clock follows the car: the GPS fix when there is one, else the simulated drive."""
from datetime import datetime, timezone

import pytest

from nova_hailo import local_clock
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.timezone import zone_for


@pytest.fixture(autouse=True)
def _clean_clock(monkeypatch):
    monkeypatch.delenv("NOVA_HAILO_TZ", raising=False)
    yield
    local_clock.set_zone_source(None)


@pytest.mark.parametrize(("lat", "lon", "zone"), [
    (37.4419, -122.1430, "America/Los_Angeles"),   # Palo Alto, the simulated start
    (12.9716, 77.5946, "Asia/Kolkata"),            # Bengaluru
    (33.4484, -112.0740, "America/Phoenix"),
    (40.7128, -74.0060, "America/New_York"),
    (51.5072, -0.1276, "Europe/London"),
])
def test_zone_for_known_regions(lat, lon, zone):
    assert zone_for(lat, lon) == zone


def test_zone_for_open_ocean_uses_longitude_offset():
    assert zone_for(-30.0, -150.0) == "Etc/GMT+10"
    assert zone_for(0.0, 2.0) == "UTC"


def test_store_zone_follows_gps_and_falls_back_to_sim():
    store = c.build_store()
    assert store.get(c.TIMEZONE) == "America/Los_Angeles"
    store.update({c.LAT: 12.9716, c.LON: 77.5946}, source="gps")
    assert store.get(c.TIMEZONE) == "Asia/Kolkata"
    # Fix lost: the sim takes the position back and the clock goes with it.
    store.release((c.LAT, c.LON))
    store.update({c.LAT: 37.4419, c.LON: -122.1430}, source="sim")
    assert store.get(c.TIMEZONE) == "America/Los_Angeles"


def test_clock_precedence(monkeypatch):
    noon_utc = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    assert "Pacific time" in local_clock.speak_local_time(now=noon_utc)   # default
    local_clock.set_zone_source(lambda: "Asia/Kolkata", fallback="America/New_York")
    assert local_clock.speak_local_time(now=noon_utc).startswith("It's 5:30 PM IST")
    local_clock.set_zone_source(lambda: None, fallback="America/New_York")
    assert "Eastern time" in local_clock.speak_local_time(now=noon_utc)
    monkeypatch.setenv("NOVA_HAILO_TZ", "Europe/London")
    assert local_clock.format_clock_stamp(now=noon_utc).endswith("13:00 BST")
