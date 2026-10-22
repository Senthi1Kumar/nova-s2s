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


def test_a_fix_keeps_position_and_speed_while_a_route_is_navigated():
    cabin, gps = feeder()
    cabin.store.set_targets({c.NAV_ACTIVE: True, c.NAV_DEST: "work"})
    gps.feed_line(FIX_GGA)
    gps.feed_line(FIX_RMC)
    cabin.store.update({c.SPEED: 50.0, c.LAT: 12.99}, source="sim")
    snap = cabin.store.snapshot(with_source=True)
    assert {snap[p][1] for p in (c.LAT, c.LON, c.HEADING, c.SPEED)} == {"gps"}
    assert snap[c.LAT][0] == pytest.approx(12.974333, abs=1e-6)


def test_a_parked_receiver_reads_zero_speed():
    cabin, gps = feeder()
    gps.feed_line(sentence("GPRMC,123520,A,1258.4600,N,07738.4900,E,0.60,213.0,111026,,,A"))
    assert cabin.store.get(c.SPEED) == 0.0


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


def _build(conf, *, gpsd=False, ports=()):
    from nova_hailo.cabingym.adapters.gps_nmea import build_gps_feeder

    values = {} if conf is None else {("cabingym", "gps"): conf}
    return build_gps_feeder(_cfg(values), CabinEnv(), gpsd_up=lambda: gpsd, serial_ports=lambda: list(ports))


def test_build_from_config():
    assert _build(None) is None                 # nothing configured, no gpsd, no receiver
    gps = _build({"enabled": True, "device": "/dev/ttyUSB9", "baud": 4800, "place_lookup": False})
    assert gps.device == "/dev/ttyUSB9" and gps.baud == 4800 and gps.place_lookup is None


def test_build_finds_a_receiver_without_config():
    from nova_hailo.cabingym.adapters.gpsd import GpsdFeeder

    assert isinstance(_build(None, gpsd=True), GpsdFeeder)
    by_id = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
    gps = _build(None, ports=[by_id])
    assert not isinstance(gps, GpsdFeeder) and gps.device == by_id and gps.baud == 9600


def test_gpsd_wins_over_a_configured_port_it_already_owns():
    from nova_hailo.cabingym.adapters.gpsd import GpsdFeeder

    assert isinstance(_build({"enabled": True, "device": "/dev/ttyUSB0"}, gpsd=True), GpsdFeeder)
    forced = _build({"source": "serial", "device": "/dev/ttyUSB0"}, gpsd=True)
    assert not isinstance(forced, GpsdFeeder) and forced.device == "/dev/ttyUSB0"
    assert isinstance(_build({"source": "gpsd"}), GpsdFeeder)          # waits for gpsd to start
    assert _build({"enabled": False}, gpsd=True) is None
    assert _build({"source": "off"}, gpsd=True, ports=["/dev/x"]) is None


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
    probes = {"gpsd_up": lambda: False, "serial_ports": lambda: []}
    gps = build_gps_feeder(_cfg({("cabingym", "gps"): {"enabled": True}}), CabinEnv(), **probes)
    assert gps.device == "/dev/ttyUSB0" and gps.baud == 9600 and callable(gps.place_lookup)
    monkeypatch.delenv("MAPBOX_API_KEY")
    gps = build_gps_feeder(_cfg({("cabingym", "gps"): {"enabled": True}}), CabinEnv(), **probes)
    assert gps.place_lookup is None


def test_where_am_i_names_the_area_under_a_fix(monkeypatch):
    import nova_hailo.cabingym.devices.drive as drive
    from nova_hailo.cabingym.executor import VehicleExecutor
    from nova_hailo.cabingym.router_regex import route_vehicle_regex

    monkeypatch.setattr(drive, "reverse_street", lambda lat, lon: None)
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


# ---- gpsd: the receiver shared with cgps, xgps and the cockpit ----

TPV = ('{"class":"TPV","device":"/dev/ttyUSB0","mode":3,"lat":12.974333,"lon":77.6415,'
       '"alt":920.0,"track":45.0,"speed":5.556}')
TPV_NO_FIX = '{"class":"TPV","device":"/dev/ttyUSB0","mode":1}'
SKY = ('{"class":"SKY","satellites":[{"PRN":1,"used":true},{"PRN":3,"used":true},'
       '{"PRN":7,"used":false}]}')


def gpsd_feeder(**kw):
    from nova_hailo.cabingym.adapters.gpsd import GpsdFeeder

    cabin = CabinEnv()
    return cabin, GpsdFeeder(cabin, **kw)


def test_gpsd_reports_feed_the_same_signals():
    cabin, gps = gpsd_feeder()
    gps.feed_json(SKY)
    gps.feed_json(TPV)
    snap = cabin.store.snapshot(with_source=True)
    assert snap[c.LAT] == (pytest.approx(12.974333, abs=1e-6), "gps")
    assert snap[c.SPEED] == (pytest.approx(20.0, abs=0.1), "gps")   # m/s from gpsd
    assert snap[c.HEADING] == (45.0, "gps")
    assert cabin.store.get(c.GPS_FIX) is True and cabin.store.get(c.GPS_SATS) == 2
    gps.feed_json(TPV_NO_FIX)
    assert cabin.store.get(c.GPS_FIX) is False
    assert cabin.store.snapshot(with_source=True)[c.LAT][1] == "sim"


def test_gpsd_noise_is_ignored():
    cabin, gps = gpsd_feeder()
    for line in ('{"class":"VERSION","release":"3.25"}', "not json", '{"class":"TPV","mode":3}', ""):
        gps.feed_json(line)
    assert cabin.store.get(c.GPS_FIX) is False and cabin.store.get(c.LAT) == 37.4419


class FakeSock:
    def __init__(self, lines, gps):
        self.lines, self.gps, self.sent = list(lines), gps, []

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, *a, **kw):
        return self

    def readline(self):
        if not self.lines:
            self.gps._stop.set()
            return ""
        return self.lines.pop(0) + "\n"

    def close(self):
        pass


def test_the_gpsd_reader_asks_for_json_and_reads_reports():
    holder = {}
    cabin, gps = gpsd_feeder(connect=lambda host, port: holder.setdefault(
        "sock", FakeSock(['{"class":"VERSION"}', SKY, TPV], holder["gps"])))
    holder["gps"] = gps
    gps.run()
    assert b'?WATCH={"enable":true,"json":true}' in holder["sock"].sent[0]
    assert cabin.store.get(c.GPS_FIX) is True and cabin.store.get(c.GPS_SATS) == 2


# ---- a parked receiver: course and position noise do not move the car ----

def _rmc(lat_min: str, knots: str, course: str) -> str:
    return sentence(f"GPRMC,123520,A,{lat_min},N,07738.4900,E,{knots},{course},111026,,,A")


def test_a_parked_receiver_keeps_the_last_heading():
    cabin, gps = feeder()
    gps.feed_line(_rmc("1258.4600", "10.80", "045.0"))          # driving north-east at 20 km/h
    for course in ("212.0", "018.0", "274.0"):                   # parked: course is noise
        gps.feed_line(_rmc("1258.4600", "0.40", course))
    assert cabin.store.get(c.HEADING) == 45.0


def test_a_parked_receiver_holds_its_position_until_it_really_moves():
    cabin, gps = feeder()
    gps.feed_line(_rmc("1258.4600", "0.00", "000.0"))
    held = cabin.store.get(c.LAT)
    gps.feed_line(_rmc("1258.4650", "0.20", "000.0"))           # ~9 m of wander while parked
    assert cabin.store.get(c.LAT) == held
    gps.feed_line(_rmc("1258.4900", "0.20", "000.0"))           # ~55 m: a real move, accepted
    assert cabin.store.get(c.LAT) != held
    gps.feed_line(_rmc("1258.5000", "10.80", "000.0"))          # driving: every fix moves the car
    assert cabin.store.get(c.LAT) == pytest.approx(12.975, abs=1e-6)
