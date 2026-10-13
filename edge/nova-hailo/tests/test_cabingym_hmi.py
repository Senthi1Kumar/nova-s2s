"""Touchscreen writes go through the same gate as voice; the event policy decides what Nova says."""
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.executor import VehicleExecutor
from nova_hailo.cabingym.hmi import EventPolicy, hmi_set
from nova_hailo.cabingym.runtime import CabinEnv

DRIVER_TEMP = c.hvac_temp("Driver")


def setup():
    cabin = CabinEnv()
    executor = VehicleExecutor(cabin)
    said = []
    policy = EventPolicy(cabin, executor, announce=said.append)
    return cabin, executor, policy, said


def test_a_temperature_touch_runs_through_the_executor():
    cabin, executor, policy, said = setup()
    out = hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 23}, policy=policy)
    assert out["ok"] is True and out["status"] == "success"
    assert cabin.store.get(DRIVER_TEMP) == 23.0
    assert out["trace"]["router"] == "hmi"
    assert said == []


def test_aliases_work_too():
    cabin, executor, policy, _ = setup()
    assert hmi_set(cabin, executor, {"alias": "volume", "value": 25}, policy=policy)["ok"]
    assert cabin.store.get(c.resolve("volume")) == 25


def test_bad_requests_are_refused_with_a_reason():
    cabin, executor, policy, _ = setup()
    assert hmi_set(cabin, executor, {"path": c.SPEED, "value": 50})["error"] == "not_writable"
    assert hmi_set(cabin, executor, {"path": "Vehicle.Nope", "value": 1})["error"] == "unknown_path"
    assert hmi_set(cabin, executor, {"path": DRIVER_TEMP})["error"] == "missing_value"
    assert hmi_set(cabin, executor, {"value": 1})["error"] == "missing_path"


def test_the_gate_still_applies_to_touch():
    cabin, executor, policy, _ = setup()
    cabin.store.apply({c.SPEED: 60.0, c.GEAR: "D"})
    out = hmi_set(cabin, executor, {"alias": "lock.driver", "value": False}, policy=policy)
    assert out["ok"] is False and out["error"] == "refused"
    assert cabin.store.get(c.resolve("lock.driver")) is True


def test_cold_setting_is_announced_with_an_offer_that_yes_accepts():
    cabin, executor, policy, said = setup()
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 19}, policy=policy)
    assert len(said) == 1 and said[0].endswith("?")
    assert executor.pending is not None
    out = executor.resolve_confirm(True)
    assert out["status"] == "success"
    assert cabin.store.get(c.resolve("seat.driver.heat")) == 2


def test_the_rule_fires_on_crossing_not_on_every_touch():
    cabin, executor, policy, said = setup()
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 19}, policy=policy)
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 18}, policy=policy)
    assert len(said) == 1
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 21}, policy=policy)
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 19}, policy=policy)
    assert len(said) == 2


def test_no_seat_offer_when_the_seat_is_already_warm():
    cabin, executor, policy, said = setup()
    cabin.store.apply({c.resolve("seat.driver.heat"): 2})
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 19}, policy=policy)
    assert len(said) == 1 and not said[0].endswith("?")
    assert executor.pending is None


def test_ordinary_touches_are_silent_context():
    cabin, executor, policy, said = setup()
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 23}, policy=policy)
    assert said == []
    assert cabin.recent_events[-1] == "On the touchscreen, the driver set driver temperature to 23."


def test_the_backend_planner_sees_recent_touchscreen_events():
    from nova_hailo.edge_harness import tasks as tk
    from nova_hailo.edge_harness.backend_agent import BackendAgent

    cabin, executor, policy, _ = setup()
    hmi_set(cabin, executor, {"path": DRIVER_TEMP, "value": 23}, policy=policy)
    seen = []

    class LLM:
        last_tool_calls = []

        def generate(self, messages, **kw):
            seen.append(messages[1]["content"])
            return "Done.", None

    reg = tk.TaskRegistry(start=lambda fn: fn())
    reg.submit("delegate", "x", lambda h: BackendAgent(LLM(), cabin, executor).run("x", h))
    assert "touchscreen" in seen[0]
