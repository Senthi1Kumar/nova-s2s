"""Real GPS (NMEA over USB serial) feeding CabinEnv, with the simulated drive as fallback."""
import pytest

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.adapters.gps_nmea import GpsFeeder, nmea_checksum_ok, parse_nmea
from nova_hailo.cabingym.runtime import CabinEnv


def sentence(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}"


# What the Pi's Adafruit GPS prints indoors (no fix) and outdoors (a fix in Bengaluru).
NO_FIX = [sentence("GPGGA,123519,,,,,0,00,,,M,,M,,"), sentence("GPRMC,123519,V,,,,,,,111026,,,N")]
FIX_RMC = sentence("GPRMC,123520,A,1258.4600,N,07738.4900,E,10.80,045.0,111026,,,A")
FIX_GGA = sentence("GPGGA,123520,1258.4600,N,07738.4900,E,1,07,0.9,920.0,M,-86.0,M,,")


def test_checksum_and_parsing():
    assert nmea_checksum_ok(FIX_RMC)
    assert not nmea_checksum_ok(FIX_RMC[:-2] + "00")
    assert parse_nmea(FIX_RMC[:-2] + "00") is None
    rmc = parse_nmea(FIX_RMC)
    assert rmc["type"] == "RMC" and rmc["valid"] is True
    assert rmc["lat"] == pytest.approx(12.974333, abs=1e-6)
    assert rmc["lon"] == pytest.approx(77.641500, abs=1e-6)
    assert rmc["speed_kph"] == pytest.approx(20.0, abs=0.01)
    assert rmc["course"] == 45.0
    gga = parse_nmea(FIX_GGA)
    assert gga["type"] == "GGA" and gga["quality"] == 1 and gga["sats"] == 7
    assert parse_nmea(NO_FIX[0])["quality"] == 0
    assert parse_nmea(NO_FIX[1])["valid"] is False
    assert parse_nmea("$GPGSV,3,1,11,...") is None   # unsupported or broken lines are skipped


def test_south_and_west_are_negative():
    rmc = parse_nmea(sentence("GPRMC,1,A,3351.0000,S,15112.0000,W,0.0,0.0,111026,,,A"))
    assert rmc["lat"] < 0 and rmc["lon"] < 0


def feeder(**kw):
    cabin = CabinEnv()
    return cabin, GpsFeeder(cabin, **kw)


def test_no_fix_leaves_the_simulated_position_alone():
    cabin, gps = feeder()
    for line in NO_FIX:
        gps.feed_line(line)
    assert cabin.store.get(c.GPS_FIX) is False and cabin.store.get(c.GPS_SATS) == 0
    assert cabin.store.get(c.LAT) == 37.4419
    assert cabin.store.snapshot(with_source=True)[c.LAT][1] == "sim"
    assert cabin.store.get(c.PLACE) == "Palo Alto"


def test_a_fix_takes_over_position_and_the_sim_cannot_overwrite_it():
    cabin, gps = feeder()
    gps.feed_line(FIX_GGA)
    gps.feed_line(FIX_RMC)
    snap = cabin.store.snapshot(with_source=True)
    assert snap[c.LAT] == (pytest.approx(12.974333, abs=1e-6), "gps")
    assert snap[c.LON][1] == "gps" and snap[c.SPEED] == (pytest.approx(20.0, abs=0.1), "gps")
    assert snap[c.HEADING] == (45.0, "gps")
    assert cabin.store.get(c.GPS_FIX) is True and cabin.store.get(c.GPS_SATS) == 7
    cabin.store.update({c.LAT: 37.0}, source="sim")
    assert cabin.store.get(c.LAT) == pytest.approx(12.974333, abs=1e-6)


def test_the_simulated_drive_owns_position_and_speed_while_it_moves():
    cabin = CabinEnv()
    moving = [False]
    gps = GpsFeeder(cabin, simulating=lambda: moving[0])
    gps.feed_line(FIX_RMC)
    snap = cabin.store.snapshot(with_source=True)
    assert snap[c.SPEED][1] == "gps" and snap[c.LAT][1] == "gps"
    moving[0] = True                            # a route starts playing
    gps.feed_line(FIX_GGA)
    gps.feed_line(FIX_RMC)
    snap = cabin.store.snapshot(with_source=True)
    assert {snap[p][1] for p in (c.LAT, c.LON, c.HEADING, c.SPEED)} == {"sim"}
    assert cabin.store.get(c.GPS_FIX) is True and cabin.store.get(c.GPS_SATS) == 7
    cabin.store.update({c.SPEED: 50.0, c.LAT: 12.99}, source="sim")
    assert cabin.store.get(c.SPEED) == 50.0 and cabin.store.get(c.LAT) == 12.99
    moving[0] = False                           # arrived: the real position comes back
    gps.feed_line(FIX_RMC)
    snap = cabin.store.snapshot(with_source=True)
    assert snap[c.LAT] == (pytest.approx(12.974333, abs=1e-6), "gps")


def test_losing_the_fix_hands_position_back_to_the_simulation():
    cabin, gps = feeder()
    gps.feed_line(FIX_GGA)
    gps.feed_line(FIX_RMC)
    for line in NO_FIX:
        gps.feed_line(line)
    snap = cabin.store.snapshot(with_source=True)
    assert cabin.store.get(c.GPS_FIX) is False
    assert snap[c.LAT] == (pytest.approx(12.974333, abs=1e-6), "sim")   # last real position kept
    cabin.store.update({c.LAT: 12.98}, source="sim")
    assert cabin.store.get(c.LAT) == 12.98


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_place_is_looked_up_once_per_500_m_or_60_s_and_kept_offline():
    calls = []
    answers = ["Indiranagar, Bengaluru", "Domlur, Bengaluru", None]

    def lookup(lat, lon):
        calls.append((lat, lon))
        return answers[len(calls) - 1]

    clock = Clock()
    cabin, gps = feeder(place_lookup=lookup, clock=clock)
    gps.feed_line(FIX_GGA)
    gps.feed_line(FIX_RMC)
    assert cabin.store.get(c.PLACE) == "Indiranagar, Bengaluru" and len(calls) == 1
    clock.t += 120
    gps.feed_line(FIX_RMC)                      # same spot: no new lookup
    assert len(calls) == 1
    moved = sentence("GPRMC,123620,A,1258.7600,N,07738.4900,E,10.80,045.0,111026,,,A")  # ~550 m north
    clock.t += 10
    gps.feed_line(moved)                        # moved, but within 60 s of... the last lookup was 130 s ago
    assert len(calls) == 2 and cabin.store.get(c.PLACE) == "Domlur, Bengaluru"
    far = sentence("GPRMC,123720,A,1259.2600,N,07738.4900,E,10.80,045.0,111026,,,A")
    clock.t += 70
    gps.feed_line(far)                          # lookup fails (offline): last value kept
    assert len(calls) == 3 and cabin.store.get(c.PLACE) == "Domlur, Bengaluru"


def test_place_lookups_are_not_repeated_within_60_s_even_when_moving():
    calls = []
    clock = Clock()
    cabin, gps = feeder(place_lookup=lambda la, lo: calls.append(1) or "Somewhere", clock=clock)
    gps.feed_line(FIX_RMC)
    clock.t += 30
    gps.feed_line(sentence("GPRMC,123620,A,1258.7600,N,07738.4900,E,10.80,045.0,111026,,,A"))
    assert len(calls) == 1


def _cfg(values):
    return lambda section, key, default=None: values.get((section, key), default)


def test_build_from_config():
    from nova_hailo.cabingym.adapters.gps_nmea import build_gps_feeder

    cabin = CabinEnv()
    assert build_gps_feeder(_cfg({}), cabin) is None
    gps = build_gps_feeder(_cfg({("cabingym", "gps"): {"enabled": True, "device": "/dev/ttyUSB9",
                                                        "baud": 4800, "place_lookup": False}}), cabin)
    assert gps.device == "/dev/ttyUSB9" and gps.baud == 4800 and gps.place_lookup is None


def test_a_receiver_that_goes_quiet_loses_its_fix():
    clock = Clock()
    cabin, gps = feeder(clock=clock)
    gps.feed_line(FIX_RMC)
    clock.t += 2
    gps.check_stale()
    assert cabin.store.get(c.GPS_FIX) is True
    clock.t += 2
    gps.check_stale()
    assert cabin.store.get(c.GPS_FIX) is False
    assert cabin.store.snapshot(with_source=True)[c.LAT][1] == "sim"


def test_place_name_from_a_mapbox_reverse_answer():
    from nova_hailo.cabingym.adapters.gps_nmea import place_name

    ctx = {"neighborhood": {"name": "Indiranagar"}, "place": {"name": "Bengaluru"},
           "region": {"name": "Karnataka"}}
    assert place_name({"features": [{"properties": {"name": "12th Main", "context": ctx}}]}) \
        == "Indiranagar, Bengaluru"
    only_city = {"features": [{"properties": {"name": "Bengaluru",
                                              "context": {"place": {"name": "Bengaluru"}}}}]}
    assert place_name(only_city) == "Bengaluru"
    assert place_name({"features": []}) is None


class FakePort:
    def __init__(self, lines, gps):
        self.lines, self.gps = list(lines), gps

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def readline(self):
        if not self.lines:
            self.gps._stop.set()
            return b""
        return (self.lines.pop(0) + "\r\n").encode()


def test_the_reader_feeds_port_lines_and_survives_garbage():
    cabin = CabinEnv()
    holder = {}
    gps = GpsFeeder(cabin, opener=lambda dev, baud: FakePort(
        ["\x00\xffjunk", "$GPGSV,3,1,11*00", FIX_GGA, FIX_RMC], holder["gps"]))
    holder["gps"] = gps
    gps.run()
    assert cabin.store.get(c.GPS_FIX) is True and cabin.store.get(c.GPS_SATS) == 7


def test_a_missing_receiver_is_retried_quietly(caplog):
    cabin = CabinEnv()
    tries = []

    def opener(dev, baud):
        tries.append(dev)
        if len(tries) >= 3:
            gps._stop.set()
        raise OSError("no such device")

    gps = GpsFeeder(cabin, opener=opener, retry_s=0.0)
    with caplog.at_level("WARNING"):
        gps.run()
    assert len(tries) == 3
    assert sum("unavailable" in r.getMessage() for r in caplog.records) == 1


def test_build_uses_the_mapbox_key_for_place_lookups(monkeypatch):
    from nova_hailo.cabingym.adapters.gps_nmea import build_gps_feeder

    monkeypatch.delenv("MAPBOX_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("MAPBOX_API_KEY", "pk.test")
    gps = build_gps_feeder(_cfg({("cabingym", "gps"): {"enabled": True}}), CabinEnv())
    assert gps.device == "/dev/ttyUSB0" and gps.baud == 9600 and callable(gps.place_lookup)
    monkeypatch.delenv("MAPBOX_API_KEY")
    gps = build_gps_feeder(_cfg({("cabingym", "gps"): {"enabled": True}}), CabinEnv())
    assert gps.place_lookup is None


def test_where_am_i_names_the_area_under_a_fix():
    from nova_hailo.cabingym.executor import VehicleExecutor
    from nova_hailo.cabingym.router_regex import route_vehicle_regex

    cabin, gps = feeder(place_lookup=lambda la, lo: "Indiranagar, Bengaluru")
    gps.feed_line(FIX_RMC)
    out = VehicleExecutor(cabin).run(route_vehicle_regex("where am I", cabin))
    assert out["speak"] == "You're in Indiranagar, Bengaluru, heading northeast."


def test_speed_from_a_receiver_without_a_fix_is_ignored():
    # What the receiver prints while acquiring: time and date valid, status V, junk speed.
    cabin, gps = feeder()
    gps.feed_line(sentence("GPRMC,140529.000,V,,,,,312.45,201.30,111026,,,N"))
    gps.feed_line(sentence("GPVTG,201.30,T,,M,312.45,N,578.65,K,N"))
    snap = cabin.store.snapshot(with_source=True)
    assert snap[c.SPEED] == (0.0, "sim") and snap[c.HEADING] == (0.0, "sim")
    assert cabin.store.get(c.GPS_FIX) is False
