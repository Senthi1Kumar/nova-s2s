# tests/test_cabingym_env.py
from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.env import CabinGymEnv, Task, Turn, load_tasks, run_oracle, run_task
from nova_hailo.cabingym.router_regex import route_vehicle_regex
from nova_hailo.cabingym.scoring import StepLike, aggregate, score_task  # noqa: F401
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall

SCORED = c.scored_paths(c.build_store())


def test_seed_tasks_load_and_resolve():
    tasks = load_tasks()
    assert len(tasks) == 89
    assert len({t.id for t in tasks}) == len(tasks)
    for t in tasks:
        assert all(k.startswith("Vehicle.") for k in {**t.init, **t.expected_diff}), t.id
        assert t.turns


def test_oracle_scores_perfectly():
    scores = []
    for task in load_tasks():
        init, final, infos = run_oracle(task)
        scores.append(score_task(task, init, final, infos, SCORED))
    agg = aggregate(scores)
    assert agg["acc"] == 1.0 and agg["gate_acc"] == 1.0


def test_regex_router_passes_every_demo_task():
    failures = []
    for task in load_tasks():
        if "demo" not in task.tags:
            continue
        init, final, infos = run_task(CabinGymEnv(route_vehicle_regex), task)
        sc = score_task(task, init, final, infos, SCORED)
        if not (sc.acc and sc.gate_ok):
            failures.append((task.id, sc.wrong_paths, [i.verdicts for i in infos]))
    assert failures == []


def test_collateral_write_costs_f1_negative_and_acc():
    task = Task("t", "S-S", (), {}, (Turn("x", None),), {c.hvac_temp("Driver"): 23.0})
    init = c.build_store().snapshot()
    final = {**init, c.hvac_temp("Driver"): 23.0, c.SUNROOF: 100}
    sc = score_task(task, init, final, [], SCORED)
    assert sc.f1_pos < 1.0 and sc.f1_neg < 1.0 and not sc.acc
    assert sc.wrong_paths == [c.SUNROOF]


def test_missing_write_costs_f1_positive():
    task = Task("t", "S-S", (), {}, (Turn("x", None),), {c.hvac_temp("Driver"): 23.0})
    init = c.build_store().snapshot()
    sc = score_task(task, init, dict(init), [], SCORED)
    # Negative-class F1: the untouched path counts as a wrong "stay" prediction too.
    assert sc.f1_pos == 0.0 and 0.9 < sc.f1_neg < 1.0 and not sc.acc
    assert sc.wrong_paths == [c.hvac_temp("Driver")]


def test_env_confirmation_flow():
    env = CabinGymEnv(route_vehicle_regex)
    env.reset()
    env.cabin.store.apply({c.SPEED: 100.0})
    _, info = env.step("open the sunroof")
    assert info.kind == "vehicle" and info.verdicts == ["confirm_first"]
    obs, info = env.step("yes")
    assert info.kind == "confirm" and info.verdicts == ["execute"]
    assert obs["snapshot"][c.SUNROOF] == 100 and obs["pending"] is False


def test_env_none_and_latency():
    env = CabinGymEnv(lambda text, cabin: None)
    env.reset()
    _, info = env.step("hello")
    assert info.kind == "none" and info.verdicts == ["none"]
    assert set(info.latency_ms) == {"route", "exec"}


def test_env_uses_custom_router_output():
    def router(text, cabin):
        return RouteOutcome("vehicle", [VehicleCall("media", "set_volume", {"value": 5})])
    env = CabinGymEnv(router)
    env.reset()
    obs, _ = env.step("anything")
    assert obs["snapshot"][c.VOLUME] == 5
