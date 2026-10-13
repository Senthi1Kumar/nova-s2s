"""Mapbox Directions v5 → one RoutePlan: totals, legs, steps, voice prompts, traffic."""
import pytest

from nova_hailo.cabingym.directions import (
    RoutePlan,
    clear_cache,
    mapbox_planner,
    plan_from_mapbox,
    straight_plan,
)
from nova_hailo.cabingym.drive import encode_polyline

A, B, C = (37.4419, -122.1430), (37.4519, -122.1430), (37.4619, -122.1430)


def _step(lat, lon, kind, modifier, name, dist, dur, voice=()):
    return {"distance": dist, "duration": dur, "name": name,
            "maneuver": {"type": kind, "modifier": modifier, "location": [lon, lat],
                         "instruction": f"{kind.title()} {modifier} onto {name}".strip()},
            "voiceInstructions": [{"distanceAlongGeometry": d, "announcement": t} for d, t in voice]}


def _payload(alternatives=0):
    route = {
        "geometry": encode_polyline([A, B, C]), "duration": 300.0, "distance": 2224.0,
        "legs": [
            {"distance": 1112.0, "duration": 120.0,
             "steps": [_step(*A, "depart", "", "Main St", 1112.0, 120.0,
                             [(1000.0, "Drive north on Main St"), (200.0, "In 200 metres, you will arrive")]),
                       _step(*B, "arrive", "", "Main St", 0.0, 0.0)],
             "annotation": {"distance": [1112.0], "duration": [120.0],
                            "maxspeed": [{"speed": 40, "unit": "km/h"}], "congestion": ["low"]}},
            {"distance": 1112.0, "duration": 180.0,
             "steps": [_step(*B, "turn", "right", "Broadway", 1112.0, 180.0,
                             [(300.0, "In 300 metres, turn right onto Murray Street"),
                              (40.0, "Turn right")]),
                       _step(*C, "arrive", "", "Broadway", 0.0, 0.0)],
             "annotation": {"distance": [1112.0], "duration": [180.0],
                            "maxspeed": [{"unknown": True}], "congestion": ["heavy"]}},
        ],
    }
    alts = [{"geometry": encode_polyline([A, C]), "duration": 400.0 + i, "distance": 2500.0}
            for i in range(alternatives)]
    return {"code": "Ok", "routes": [route, *alts]}


def test_a_plan_has_totals_legs_steps_and_traffic():
    plan = plan_from_mapbox(_payload())
    assert isinstance(plan, RoutePlan) and plan.geometry == "mapbox"
    assert plan.duration_s == 300.0 and plan.distance_m == 2224.0
    assert len(plan.points) == 3
    assert plan.leg_ends_km == pytest.approx([1.112, 2.224], abs=0.01)
    assert plan.congestion == "lh"                          # one char per segment
    assert [s["type"] for s in plan.steps] == ["depart", "arrive", "turn", "arrive"]
    turn = plan.steps[2]
    assert turn["modifier"] == "right" and turn["street"] == "Broadway"
    assert turn["lat"] == pytest.approx(B[0]) and turn["leg"] == 1
    assert turn["start_km"] == pytest.approx(1.112, abs=0.01)


def test_remaining_time_and_distance_come_from_the_route_not_the_cruise_speed():
    plan = plan_from_mapbox(_payload())
    assert plan.remaining(0.0) == pytest.approx((300.0, 2.224), abs=0.5)
    secs, km = plan.remaining(1.668)                        # half-way through leg 2
    assert secs == pytest.approx(90.0, abs=2) and km == pytest.approx(0.556, abs=0.01)
    assert plan.remaining(99.0) == (0.0, 0.0)


def test_speed_limit_and_next_maneuver_follow_the_car():
    plan = plan_from_mapbox(_payload())
    assert plan.speed_limit_kph(0.5) == 40 and plan.speed_limit_kph(1.6) is None
    assert plan.next_step(0.5) == 1                         # the arrive at B comes next
    assert plan.next_step(1.2) == 3


def test_voice_prompts_trigger_before_their_maneuver():
    plan = plan_from_mapbox(_payload())
    prompts = [(round(v["at_km"], 3), v["text"]) for v in plan.voice]
    assert ("In 300 metres, turn right onto Murray Street" in [t for _, t in prompts])
    at = dict((t, k) for k, t in prompts)
    assert at["In 300 metres, turn right onto Murray Street"] == pytest.approx(2.224 - 0.3, abs=0.01)
    assert all(v["until_km"] > v["at_km"] for v in plan.voice)


def test_alternatives_only_without_stops():
    plan = plan_from_mapbox(_payload(alternatives=2))
    assert len(plan.alternatives) == 2
    assert set(plan.alternatives[0]) == {"route", "duration_s", "distance_m"}


def test_the_planner_makes_one_call_with_every_waypoint_and_caches_it():
    clear_cache()
    calls = []

    def fetch(url, params):
        calls.append((url, dict(params)))
        return _payload()

    plan = mapbox_planner("pk.x", fetch=fetch)
    first = plan([A, B, C], exclude=("toll",))
    assert first is not None and len(calls) == 1
    url, params = calls[0]
    assert "/driving-traffic/" in url and f"{A[1]:.6f},{A[0]:.6f};" in url
    assert params["steps"] == "true" and params["voice_instructions"] == "true"
    assert "congestion" in params["annotations"] and params["exclude"] == "toll"
    assert params["alternatives"] == "false"                # stops: no alternatives
    again = plan([(A[0] + 0.0004, A[1]), B, C], exclude=("toll",))   # car moved ~45 m
    assert again is first and len(calls) == 1


def test_a_failed_call_hides_the_token_and_returns_none(caplog):
    clear_cache()

    def fetch(url, params):
        raise RuntimeError(f"401 for {url}?access_token={params['access_token']}")

    with caplog.at_level("INFO"):
        assert mapbox_planner("pk.secret", fetch=fetch)([A, C]) is None
    assert "pk.secret" not in caplog.text


def test_a_straight_plan_estimates_from_cruise_speed():
    plan = straight_plan([A, B, C], legs=2, cruise_kph=60.0)
    assert plan.geometry == "straight" and plan.steps == [] and plan.congestion == ""
    assert plan.duration_s == pytest.approx(plan.distance_m / 1000 / 60 * 3600, rel=0.01)
    assert len(plan.leg_ends_km) == 2


def test_one_approach_prompt_per_maneuver():
    from nova_hailo.cabingym.directions import spoken_prompts

    turn = [{"distanceAlongGeometry": 1600.0, "announcement": "In 1 mile, turn right onto Castro Street"},
            {"distanceAlongGeometry": 300.0, "announcement": "In 300 metres, turn right onto Castro Street"},
            {"distanceAlongGeometry": 40.0, "announcement": "Turn right onto Castro Street"}]
    # The 1 mile prompt opens this 1.6 km step; one approach prompt follows it.
    assert [t for _, t in spoken_prompts(turn, 1600.0)] == [
        "In 1 mile, turn right onto Castro Street", "In 300 metres, turn right onto Castro Street"]
    # On a long step the countdown is not the opener: only the approach is kept.
    assert [t for _, t in spoken_prompts(turn, 5000.0)] == ["In 300 metres, turn right onto Castro Street"]
    assert spoken_prompts([{"distanceAlongGeometry": 30.0, "announcement": "Turn left"}], 30.0) == [
        (30.0, "Turn left")]
    assert spoken_prompts([{"distanceAlongGeometry": 30.0, "announcement": ""}], 30.0) == []
