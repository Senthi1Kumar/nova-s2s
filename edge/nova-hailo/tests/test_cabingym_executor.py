# tests/test_cabingym_executor.py
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.executor import VehicleExecutor
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def setup(**init):
    cabin = CabinEnv()
    if init:
        cabin.store.apply({c.resolve(k): v for k, v in init.items()})
    clock = Clock()
    return cabin, VehicleExecutor(cabin, clock=clock), clock


def vehicle(*calls):
    return RouteOutcome("vehicle", list(calls), trace={"router": "test"})


def test_execute_writes_store_and_reports_diff():
    cabin, ex, _ = setup()
    out = ex.run(vehicle(VehicleCall("hvac", "set_temperature", {"zone": "driver", "value": 23.0})))
    assert out["ok"] and out["name"] == "vehicle" and out["status"] == "success"
    assert out["speak"] == "Driver side set to 23 degrees."
    assert cabin.store.get(c.hvac_temp("Driver")) == 23.0
    assert out["result"]["verdicts"] == ["execute"]
    assert out["result"]["diff"] == [{"path": c.hvac_temp("Driver"), "before": 21.0, "after": 23.0}]
    trace = out["result"]["trace"]
    assert trace["router"] == "test" and trace["gate"] == ["execute"]


def test_refuse_leaves_store_untouched():
    cabin, ex, _ = setup(speed=60.0)
    out = ex.run(vehicle(VehicleCall("doors", "lock_doors", {"locked": False})))
    assert out["status"] == "refused" and out["result"]["diff"] == []
    assert cabin.store.get(c.resolve("lock.driver")) is True


def test_confirm_then_yes():
    cabin, ex, _ = setup(speed=100.0)
    out = ex.run(vehicle(VehicleCall("body", "set_sunroof", {"value": 100})))
    assert out["status"] == "confirm" and ex.pending is not None
    assert cabin.store.get(c.SUNROOF) == 0
    out = ex.resolve_confirm(True)
    assert out["status"] == "success" and cabin.store.get(c.SUNROOF) == 100
    assert ex.pending is None


def test_confirm_then_no():
    cabin, ex, _ = setup(speed=100.0)
    ex.run(vehicle(VehicleCall("body", "set_sunroof", {"value": 100})))
    out = ex.resolve_confirm(False)
    assert out["status"] == "cancelled" and out["result"]["verdicts"] == ["cancelled"]
    assert cabin.store.get(c.SUNROOF) == 0


def test_confirm_expires():
    cabin, ex, clock = setup(speed=100.0)
    ex.run(vehicle(VehicleCall("body", "set_sunroof", {"value": 100})))
    clock.t += ex.confirm_ttl_s + 1.0
    assert ex.pending is None
    assert ex.resolve_confirm(True) is None
    assert cabin.store.get(c.SUNROOF) == 0


def test_new_turn_drops_pending():
    cabin, ex, _ = setup(speed=100.0)
    ex.run(vehicle(VehicleCall("body", "set_sunroof", {"value": 100})))
    ex.run(vehicle(VehicleCall("media", "set_volume", {"value": 30})))
    assert ex.pending is None
    assert ex.resolve_confirm(True) is None
    assert cabin.store.get(c.SUNROOF) == 0


def test_mixed_turn_executes_safe_part_and_holds_the_rest():
    cabin, ex, _ = setup(speed=100.0)
    out = ex.run(vehicle(VehicleCall("media", "set_volume", {"value": 30}),
                         VehicleCall("body", "set_sunroof", {"value": 100})))
    assert out["status"] == "success" and out["result"]["verdicts"] == ["execute", "confirm_first"]
    assert cabin.store.get(c.VOLUME) == 30
    assert [x.api for x in ex.pending.calls] == ["set_sunroof"]


def test_clarify_and_decline_outcomes():
    _, ex, _ = setup()
    assert ex.run(RouteOutcome("clarify", speak="Who?"))["status"] == "clarify"
    out = ex.run(RouteOutcome("decline", speak="Not here."))
    assert out["status"] == "declined" and out["result"]["verdicts"] == ["decline"]


def test_handler_clarify_and_error():
    cabin, ex, _ = setup()
    out = ex.run(vehicle(VehicleCall("phone", "answer_call", {})))
    assert out["status"] == "clarify" and out["speak"] == "There's no incoming call."
    out = ex.run(vehicle(VehicleCall("media", "set_source", {"source": "tape"})))
    assert out["status"] == "error" and out["speak"] == "I couldn't do that."
    assert cabin.store.get(c.MEDIA_SOURCE) == "streaming"


def test_gate_overrides_are_applied():
    cabin, ex, _ = setup(headlights="low")
    ex.run(vehicle(VehicleCall("system", "set_display_brightness", {"value": 5})))
    assert cabin.store.get(c.BRIGHTNESS) == 10


def test_hybrid_speaks_simulated_cabin():
    class FakeHybrid:
        name = "hybrid"

        def attach(self, store):
            store.target_sink = None

        def tick(self, dt_s):
            return []

        def settle(self):
            return []

    ex = VehicleExecutor(CabinEnv(adapter=FakeHybrid()))
    out = ex.run(vehicle(VehicleCall("hvac", "set_temperature", {"zone": "driver", "value": 21.5})))
    assert out["speak"] == "Driver side set to 21.5 degrees in the simulated cabin."
    out = ex.run(vehicle(VehicleCall("drive", "get_range", {})))
    assert "simulated" not in out["speak"]


def test_a_confirmation_waits_long_enough_for_the_question_to_be_heard_and_answered():
    from nova_hailo.cabingym.executor import VehicleExecutor
    from nova_hailo.cabingym.runtime import CabinEnv

    t = [0.0]
    ex = VehicleExecutor(CabinEnv(), clock=lambda: t[0])
    ex.offer([], ttl_s=ex.confirm_ttl_s)
    t[0] = 25.0          # the question plays, the mic waits out the echo, the driver answers
    assert ex.pending is not None
    t[0] = 31.0
    assert ex.pending is None


def test_one_side_ac_is_not_switched_for_everyone():
    from nova_hailo.cabingym import catalog as cat
    from nova_hailo.cabingym.executor import VehicleExecutor
    from nova_hailo.cabingym.router_regex import route_vehicle_regex
    from nova_hailo.cabingym.runtime import CabinEnv

    cabin = CabinEnv()
    cabin.store.set_targets({cat.AC: True})
    out = VehicleExecutor(cabin).run(route_vehicle_regex("turn off the passenger side AC", cabin))
    assert out["status"] == "clarify" and "whole cabin" in out["speak"]
    assert cabin.store.get(cat.AC) is True
