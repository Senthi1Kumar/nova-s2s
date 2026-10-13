"""Direct / delegated / mixed benchmark: the harness inside CabinGym episodes."""
import importlib.util
import json
from pathlib import Path

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.env import load_tasks
from nova_hailo.cabingym.router_regex import route_vehicle_regex
from nova_hailo.edge_harness.bench import MULTISTEP_TASKS, HarnessEpisodeEnv, fake_backend_factory

ROOT = Path(__file__).resolve().parents[1]


def _task(task_id):
    return next(t for t in load_tasks(MULTISTEP_TASKS) if t.id == task_id)


def _run(env, task):
    env.reset(task)
    infos = [env.step(t.user)[1] for t in task.turns]
    env.cabin.adapter.settle()
    return infos


def test_multistep_task_file_is_valid():
    tasks = load_tasks(MULTISTEP_TASKS)
    assert len(tasks) >= 8
    assert all("multistep" in t.tags for t in tasks)


def test_mixed_delegates_a_four_action_request_and_completes_it():
    env = HarnessEpisodeEnv(route_vehicle_regex, execution="mixed",
                            backend_factory=fake_backend_factory)
    task = _task("ms-004")
    infos = _run(env, task)
    assert infos[0].kind == "delegate"
    assert env.cabin.store.get(c.resolve("volume")) == 25
    assert env.cabin.store.get(c.resolve("hazards")) is True
    assert infos[0].latency_ms["backend"] >= 0.0


def test_offline_mixed_runs_the_parsed_part_directly():
    env = HarnessEpisodeEnv(route_vehicle_regex, execution="mixed",
                            backend_factory=fake_backend_factory, online=False)
    infos = _run(env, _task("ms-004"))
    assert infos[0].kind == "vehicle"
    assert env.cabin.store.get(c.resolve("volume")) == 25


def test_direct_mode_never_delegates():
    env = HarnessEpisodeEnv(route_vehicle_regex, execution="direct",
                            backend_factory=fake_backend_factory)
    infos = _run(env, _task("ms-004"))
    assert infos[0].kind == "vehicle"


def test_delegated_mode_sends_short_turns_to_the_backend_too():
    env = HarnessEpisodeEnv(route_vehicle_regex, execution="delegated",
                            backend_factory=fake_backend_factory)
    env.reset(None)
    _, info = env.step("set the volume to 25")
    assert info.kind == "delegate"
    assert env.cabin.store.get(c.resolve("volume")) == 25


def test_delegated_without_a_backend_is_offline():
    env = HarnessEpisodeEnv(route_vehicle_regex, execution="delegated", backend_factory=None)
    env.reset(None)
    _, info = env.step("set the volume to 25")
    assert info.kind == "offline" and info.verdicts == ["offline"]


def _runner():
    spec = importlib.util.spec_from_file_location("cabingym_eval", ROOT / "scripts/cabingym_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_reports_the_three_configurations_with_a_split(tmp_path):
    for mode in ("direct", "delegated", "mixed"):
        out = tmp_path / mode
        assert _runner().main(["--execution", mode, "--backend", "fake",
                               "--tasks", str(MULTISTEP_TASKS), "--out", str(out)]) == 0
        summary = json.loads((out / "summary.json").read_text())
        assert summary["execution"] == mode and summary["backend"] == "fake"
        assert summary["network"] == "on"
        assert set(summary["split"]) == {"multistep"}
        assert {"n", "acc", "latency_ms_p50", "latency_ms_p95"} <= set(summary["split"]["multistep"])


def test_runner_without_execution_is_unchanged(tmp_path):
    assert _runner().main(["--router", "regex", "--tag", "demo", "--out", str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["acc"] == 1.0 and "execution" not in summary
