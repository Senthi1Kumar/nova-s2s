"""Simulated drive along real road geometry, and the nova.nav feed that follows navigation."""
import pytest

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.drive import (
    NavFeed,
    _haversine_km,
    RouteBook,
    SimDrive,
    bearing_deg,
    build_nav_feed,
    decode_polyline,
    encode_polyline,
    path_length_km,
)
from nova_hailo.cabingym.runtime import CabinEnv

START = (37.4419, -122.1430)
WORK = (37.4847, -122.1477)


def test_polyline6_round_trip_and_the_classic_example():
    pts = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert encode_polyline(pts, precision=5) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    back = decode_polyline(encode_polyline(pts))
    assert all(abs(a - b) < 1e-6 for p, q in zip(pts, back) for a, b in zip(p, q))


def test_bearing_and_length():
    assert bearing_deg((0.0, 0.0), (1.0, 0.0)) == pytest.approx(0.0, abs=0.5)
    assert bearing_deg((0.0, 0.0), (0.0, 1.0)) == pytest.approx(90.0, abs=0.5)
    assert path_length_km([(0.0, 0.0), (0.0, 1.0)]) == pytest.approx(111.2, abs=0.5)


def test_routebook_uses_real_road_geometry_between_known_places():
    book = RouteBook()
    pts = book.between(START, WORK)
    assert len(pts) > 100 and book.last_geometry == "osm"
    assert path_length_km(pts) == pytest.approx(6.3, abs=0.3)
    rev = book.between(WORK, START)  # driven in the other direction
    assert rev[0] == pts[-1] and rev[-1] == pts[0]


def test_routebook_falls_back_to_a_straight_line():
    book = RouteBook()
    pts = book.between((37.40, -122.10), (37.41, -122.11))
    assert pts == [(37.40, -122.10), (37.41, -122.11)] and book.last_geometry == "straight"


def test_routebook_asks_the_live_router_before_the_straight_line():
    calls = []
    book = RouteBook(live=lambda a, b: calls.append((a, b)) or [a, (37.405, -122.105), b])
    pts = book.between((37.40, -122.10), (37.41, -122.11))
    assert len(pts) == 3 and book.last_geometry == "osrm" and calls


def _drive(**kw):
    cabin = CabinEnv()
    return cabin, SimDrive(cabin, **kw)


def test_parked_car_writes_nothing():
    cabin, drive = _drive()
    seen = []
    cabin.store.subscribe(seen.append)
    for _ in range(10):
        drive.step(0.2)
    assert seen == []


def test_the_car_drives_along_the_route_and_parks_at_the_end():
    cabin, drive = _drive(cruise_kph=60.0)
    arrived = []
    drive.on_arrive = lambda: arrived.append(True)
    pts = RouteBook().between(START, WORK)
    drive.follow(pts)
    odo0 = cabin.store.get(c.ODOMETER)
    soc0 = cabin.store.get(c.SOC)
    for _ in range(50):          # 10 s
        drive.step(0.2)
    assert cabin.store.get(c.GEAR) == "D"
    assert 30.0 < cabin.store.get(c.SPEED) <= 60.0
    assert cabin.store.get(c.LAT) != START[0]
    assert cabin.store.get(c.ODOMETER) > odo0
    for _ in range(5000):        # long enough to finish 6.3 km
        drive.step(0.2)
    assert arrived == [True]
    assert cabin.store.get(c.SPEED) == 0.0 and cabin.store.get(c.GEAR) == "P"
    assert cabin.store.get(c.LAT) == pytest.approx(pts[-1][0], abs=1e-4)
    assert cabin.store.get(c.SOC) < soc0


def test_stop_brakes_to_a_halt_where_the_car_is():
    cabin, drive = _drive(cruise_kph=50.0)
    drive.follow(RouteBook().between(START, WORK))
    for _ in range(40):
        drive.step(0.2)
    drive.stop()
    for _ in range(60):
        drive.step(0.2)
    assert cabin.store.get(c.SPEED) == 0.0 and cabin.store.get(c.GEAR) == "P"
    assert not drive.moving


def test_real_gps_position_is_never_overwritten():
    cabin, drive = _drive()
    cabin.store.update({c.LAT: 37.0, c.LON: -122.0}, source="gps")
    drive.follow(RouteBook().between(START, WORK))
    for _ in range(20):
        drive.step(0.2)
    assert cabin.store.get(c.LAT) == 37.0
    assert cabin.store.get(c.SPEED) > 0.0  # the rest of the drive still streams


def _feed(**kw):
    cabin = CabinEnv()
    drive = SimDrive(cabin, cruise_kph=50.0)
    events = []
    feed = NavFeed(cabin, drive, RouteBook(), publish=events.append, **kw)
    cabin.store.subscribe(feed.on_changes)
    return cabin, drive, feed, events


def _navigate(cabin, name, lat, lon):
    cabin.store.set_targets({c.NAV_ACTIVE: True, c.NAV_DEST: name,
                             c.NAV_DEST_LAT: lat, c.NAV_DEST_LON: lon,
                             c.NAV_ETA: 11.0, c.NAV_DIST: 6.3})


def test_navigating_starts_the_drive_and_publishes_the_route():
    cabin, drive, feed, events = _feed()
    _navigate(cabin, "work", *WORK)
    assert feed.dirty
    feed.sync()
    assert drive.moving and not feed.dirty
    ev = events[-1]
    assert ev["type"] == "nova.nav" and ev["active"] is True
    assert ev["destination"] == {"name": "work", "lat": WORK[0], "lon": WORK[1]}
    assert len(decode_polyline(ev["route"])) > 100 and ev["geometry"] == "osm"
    assert ev["eta_min"] == 11.0 and ev["distance_km"] == 6.3
    assert ev["pois"] == []


def test_stopping_navigation_parks_the_car_and_clears_the_route():
    cabin, drive, feed, events = _feed()
    _navigate(cabin, "work", *WORK)
    feed.sync()
    cabin.store.set_targets({c.NAV_ACTIVE: False, c.NAV_DEST: ""})
    feed.sync()
    assert events[-1]["active"] is False and events[-1]["route"] == ""
    for _ in range(60):
        feed.tick(0.2)
    assert cabin.store.get(c.SPEED) == 0.0


def test_arrival_ends_navigation():
    cabin, drive, feed, events = _feed()
    _navigate(cabin, "work", *WORK)
    feed.sync()
    for _ in range(5000):
        feed.tick(0.2)
    if feed.dirty:
        feed.sync()
    assert cabin.store.get(c.NAV_ACTIVE) is False
    assert events[-1]["active"] is False


def test_unrelated_changes_do_not_mark_the_feed_dirty():
    cabin, drive, feed, events = _feed()
    cabin.store.set_targets({c.hvac_temp("Driver"): 23.0})
    assert not feed.dirty and events == []


def test_autoplay_drives_between_two_places_without_a_voice_command():
    cabin, drive, feed, events = _feed(autoplay=("start", "work"))
    feed.start()
    if feed.dirty:
        feed.sync()
    assert cabin.store.get(c.NAV_ACTIVE) is True and cabin.store.get(c.NAV_DEST) == "work"
    assert drive.moving


def _cfg(values):
    return lambda section, key, default=None: values.get((section, key), default)


def test_build_nav_feed_follows_config():
    from nova_hailo.cabingym.drive import build_nav_feed

    cabin = CabinEnv()
    assert build_nav_feed(_cfg({}), cabin, publish=print) is None  # off unless enabled
    feed = build_nav_feed(_cfg({("cabingym", "sim_drive"): {
        "enabled": True, "cruise_kph": 70, "autoplay": ["start", "work"],
        "route_service": "off"}}), cabin, publish=print)
    assert feed.drive.cruise_kph == 70.0 and feed.autoplay == ("start", "work")
    assert feed.routes.live is None


def test_drive_loop_syncs_when_dirty_and_ticks_at_its_rate():
    import asyncio

    from nova_hailo.cabingym.drive import drive_loop

    cabin, drive, feed, events = _feed()
    _navigate(cabin, "work", *WORK)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 10:
            raise asyncio.CancelledError

    async def go():
        try:
            await drive_loop(feed, hz=5.0, sleep=fake_sleep, to_thread=lambda f: _run(f))
        except asyncio.CancelledError:
            pass

    async def _run(f):
        return f()

    asyncio.run(go())
    assert sleeps and all(abs(s - 0.2) < 1e-9 for s in sleeps)
    assert events and events[-1]["active"] is True
    assert cabin.store.get(c.SPEED) > 0.0


def test_live_router_reads_the_mapbox_api_key_first(monkeypatch):
    from nova_hailo.cabingym.drive import live_router_from_env

    monkeypatch.delenv("MAPBOX_API_KEY", raising=False)
    monkeypatch.delenv("MAPBOX_ACCESS_TOKEN", raising=False)
    assert live_router_from_env("auto")[1] == "osrm"
    monkeypatch.setenv("MAPBOX_ACCESS_TOKEN", "pk.legacy")
    assert live_router_from_env("auto")[1] == "mapbox"
    monkeypatch.delenv("MAPBOX_ACCESS_TOKEN")
    monkeypatch.setenv("MAPBOX_API_KEY", "pk.canonical")
    assert live_router_from_env("auto")[1] == "mapbox"
    assert live_router_from_env("off") == (None, "")


def test_router_failures_never_log_the_access_token(caplog):
    import httpx

    req = httpx.Request("GET", "https://api.mapbox.com/directions/v5/x?access_token=pk.secret")
    resp = httpx.Response(401, request=req)

    def live(a, b):
        raise httpx.HTTPStatusError(f"Client error '401 Unauthorized' for url '{req.url}'",
                                    request=req, response=resp)

    routes = RouteBook(live=live, live_name="mapbox")
    with caplog.at_level("INFO"):
        assert routes.between((0.0, 0.0), (0.1, 0.1)) == [(0.0, 0.0), (0.1, 0.1)]
    assert "pk.secret" not in caplog.text and "401" in caplog.text


BENGALURU = {"start": [12.9716, 77.5946], "home": {"lat": 12.9784, "lon": 77.6408},
             "gym": [12.9352, 77.6245]}


def test_configured_places_replace_the_palo_alto_site():
    cabin = CabinEnv()
    cabin.configure_site(BENGALURU, area="Bengaluru")
    saved = {p["name"]: (p["lat"], p["lon"]) for p in cabin.store.get(c.NAV_SAVED)}
    assert saved["home"] == (12.9784, 77.6408) and saved["gym"] == (12.9352, 77.6245)
    assert saved["work"] == (37.4847, -122.1477)   # not configured: the default stays
    assert "start" not in saved
    assert (cabin.store.get(c.LAT), cabin.store.get(c.LON)) == (12.9716, 77.5946)
    assert cabin.store.get(c.PLACE) == "Bengaluru"


def test_bad_place_entries_are_skipped():
    cabin = CabinEnv()
    cabin.configure_site({"home": [95.0, 10.0], "work": "downtown", "gym": [1.0, 2.0]})
    saved = {p["name"]: (p["lat"], p["lon"]) for p in cabin.store.get(c.NAV_SAVED)}
    assert saved["home"] == (37.4275, -122.1697) and saved["gym"] == (1.0, 2.0)


def test_the_route_book_plays_configured_places():
    values = {("cabingym", "sim_drive"): {"enabled": True, "route_service": "off",
                                          "autoplay": ["start", "home"]},
              ("cabingym", "places"): BENGALURU}
    cabin = CabinEnv()
    feed = build_nav_feed(lambda s, k, default=None: values.get((s, k), default), cabin,
                          publish=lambda e: None)
    assert feed.routes.places["home"] == (12.9784, 77.6408)
    assert feed.routes.places["start"] == (12.9716, 77.5946)


def _drive_until(feed, done, *, dt=1.0, limit=4000):
    for _ in range(limit):
        feed.tick(dt)
        if feed.dirty:
            feed.sync()
        if done():
            return True
    return False


def test_a_reached_stop_is_dropped_and_a_new_stop_routes_from_here():
    cabin = CabinEnv()
    drive = SimDrive(cabin, cruise_kph=50)
    feed = NavFeed(cabin, drive, RouteBook(), publish=lambda e: None)
    cabin.store.subscribe(feed.on_changes)
    a, b = (37.4460, -122.1550), (37.4600, -122.1500)
    cabin.store.update({c.LAT: 37.4419, c.LON: -122.1430}, source="sim")
    cabin.store.set_targets({c.NAV_ACTIVE: True, c.NAV_DEST: "work", c.NAV_DEST_LAT: 37.4847,
                             c.NAV_DEST_LON: -122.1477,
                             c.NAV_STOPS: ({"name": "A", "lat": a[0], "lon": a[1]},)})
    feed.sync()
    assert _drive_until(feed, lambda: cabin.store.get(c.NAV_STOPS) == ())
    assert cabin.store.get(c.NAV_ACTIVE) is True                    # still going to work
    here = (cabin.store.get(c.LAT), cabin.store.get(c.LON))
    cabin.store.set_targets({c.NAV_STOPS: ({"name": "B", "lat": b[0], "lon": b[1]},)})
    feed.sync()
    route = feed._route
    assert _haversine_km(route[0], here) < 0.05                      # from where the car is
    assert any(_haversine_km(p, b) < 0.05 for p in route)            # through B
    assert not any(_haversine_km(p, a) < 0.03 for p in route[1:])    # never back through A


def test_a_mapbox_plan_sets_the_eta_and_publishes_progress():
    from nova_hailo.cabingym.directions import RoutePlan

    a, b = (37.4419, -122.1430), (37.4519, -122.1430)
    plan = RoutePlan(
        geometry="mapbox", duration_s=300.0, distance_m=2224.0, points=[a, b],
        leg_ends_km=[2.224], congestion="lh",
        steps=[{
            "type": "turn", "modifier": "right", "instruction": "Turn right",
            "street": "Broadway", "distance_m": 2224.0, "duration_s": 300.0,
            "lat": b[0], "lon": b[1], "start_km": 0.4, "leg": 0,
        }],
        voice=[
            {"at_km": 0.05, "until_km": 0.3, "text": "Drive north on Main St"},
            {"at_km": 0.45, "until_km": 0.7, "text": "Turn right"},
        ],
        alternatives=[{"route": "alt", "duration_s": 400, "distance_m": 2500}],
        _leg_dur_s=[300.0], _limits=[(2.224, 40.0)],
    )
    cabin, drive, feed, events = _feed(
        planner=lambda wps, exclude=(): plan, voice_guidance=True,
    )
    _navigate(cabin, "north", *b)
    feed.sync()
    ev = events[-1]
    assert ev["geometry"] == "mapbox" and ev["eta_min"] == 5.0
    assert ev["distance_km"] == pytest.approx(2.22, abs=0.01)
    assert ev["duration_s"] == 300 and ev["distance_m"] == 2224
    assert ev["congestion"] == "lh" and ev["stops"] == []
    assert ev["steps"][0]["street"] == "Broadway" and "start_km" not in ev["steps"][0]
    assert ev["alternatives"] == [{"route": "alt", "duration_s": 400, "distance_m": 2500}]
    for _ in range(6):
        feed.tick(0.2)
    progress = [e for e in events if e["type"] == "nova.nav.progress"]
    assert progress and "active" not in progress[-1]
    assert progress[-1]["speed_limit_kph"] == 40 and progress[-1]["remaining_s"] > 250
    said = None
    for _ in range(30):
        feed.tick(1.0)
        if feed.guidance_waiting():
            said = feed.take_guidance()
            break
    assert said == "Drive north on Main St"
    for _ in range(80):
        feed.tick(1.0)
        if drive.progress_km() >= 0.75:
            break
    assert drive.progress_km() >= 0.75
    assert feed.take_guidance() is None
