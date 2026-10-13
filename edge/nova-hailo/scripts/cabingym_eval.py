#!/usr/bin/env python3
"""Run CabinGym tasks through a router and report VehicleWorld-style metrics.

  python scripts/cabingym_eval.py --router regex
  python scripts/cabingym_eval.py --router jev+regex --timeout 1.0 --out runtime/cabingym/jev
  python scripts/cabingym_eval.py --router oracle
  python scripts/cabingym_eval.py --execution mixed --backend groq --tasks nova_hailo/cabingym/tasks/multistep.jsonl
  python scripts/cabingym_eval.py --execution mixed --network off   # what the Pi does offline

--execution runs the Nova edge harness inside each episode: direct (frontend
only), delegated (backend agent for every vehicle turn) or mixed (frontend for
what it parses, backend for plans). --backend groq needs GROQ_API_KEY; fake is
a deterministic planner for CI.

jev / jev+regex need TYPESAFE_API_KEY and the optional typesafe-sdk; run
them on the Pi to measure the real two-stage latency.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from nova_hailo.cabingym import catalog
from nova_hailo.cabingym.env import CabinGymEnv, load_tasks, run_oracle, run_task
from nova_hailo.cabingym.router_regex import route_vehicle_regex
from nova_hailo.cabingym.scoring import aggregate, score_task


def _router(name: str, timeout: float):
    if name == "regex":
        return route_vehicle_regex
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    from nova_hailo.cabingym.router_jev import route_vehicle_jev

    client = TypeSafeClient(retry=RetryPolicy(max_retries=0), timeout=timeout)

    def jev(text, cabin):
        return route_vehicle_jev(text, cabin, client=client, timeout_sec=timeout)

    if name == "jev":
        return jev
    return lambda text, cabin: jev(text, cabin) or route_vehicle_regex(text, cabin)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--router", choices=("regex", "jev", "jev+regex", "oracle"), default="regex")
    ap.add_argument("--tasks", default=None)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--profile", default="ev_demo")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--execution", choices=("direct", "delegated", "mixed"), default=None)
    ap.add_argument("--backend", choices=("groq", "openrouter", "cerebras", "fake"), default="groq")
    ap.add_argument("--backend-model", default="openai/gpt-oss-120b")
    ap.add_argument("--network", choices=("on", "off"), default="on")
    ap.add_argument("--budget", type=float, default=20.0,
                    help="backend wall-time budget per delegated turn (s); raise it on a "
                         "rate-limited free tier so waits do not fail the task")
    args = ap.parse_args(argv)

    tasks = [t for t in load_tasks(args.tasks) if args.tag is None or args.tag in t.tags]
    scored = catalog.scored_paths(catalog.build_store(args.profile))
    router = None if args.router == "oracle" else _router(args.router, args.timeout)
    factory = None
    if args.execution is not None:
        if router is None:
            ap.error("--execution needs a real router, not oracle")
        from nova_hailo.edge_harness import bench

        if args.network == "on":
            try:
                factory = (bench.fake_backend_factory if args.backend == "fake"
                           else bench.groq_backend_factory(args.backend_model, args.budget,
                                                           provider=args.backend))
            except RuntimeError as exc:
                print(f"backend unavailable: {exc}", file=sys.stderr)
                return 2
    scores, rows, route_ms, task_ms = [], [], [], {}
    for task in tasks:
        if router is None:
            init, final, infos = run_oracle(task, args.profile)
        elif args.execution is not None:
            env = bench.HarnessEpisodeEnv(router, execution=args.execution, backend_factory=factory,
                                          online=args.network == "on", profile=args.profile)
            init, final, infos = run_task(env, task)
        else:
            init, final, infos = run_task(CabinGymEnv(router, profile=args.profile), task)
        sc = score_task(task, init, final, infos, scored)
        scores.append(sc)
        route_ms += [i.latency_ms.get("route", 0.0) for i in infos]
        task_ms[task.id] = sum(i.latency_ms.get("route", 0.0) + i.latency_ms.get("exec", 0.0)
                               for i in infos)
        rows.append({"task": task.id, "tags": list(task.tags), "score": asdict(sc),
                     "turns": [{"user": t.user, "kind": i.kind, "verdicts": i.verdicts,
                                "speak": i.speak, "diff": i.diff, "latency_ms": i.latency_ms}
                               for t, i in zip(task.turns, infos)]})
    summary = {**aggregate(scores), "router": args.router, "profile": args.profile,
               "route_ms_p50": _pct(route_ms, 0.5), "route_ms_p95": _pct(route_ms, 0.95)}
    if args.execution is not None:
        summary.update({"execution": args.execution, "backend": args.backend,
                        "network": args.network, "split": {}})
        groups = {"multistep": [t for t in tasks if "multistep" in t.tags],
                  "short": [t for t in tasks if "multistep" not in t.tags]}
        by_id = {sc.task_id: sc for sc in scores}
        for name, group in groups.items():
            if not group:
                continue
            ms = [task_ms[t.id] for t in group]
            summary["split"][name] = {
                **aggregate([by_id[t.id] for t in group]),
                "latency_ms_p50": _pct(ms, 0.5), "latency_ms_p95": _pct(ms, 0.95),
            }
    print(f"router={args.router} n={summary['n']} acc={summary['acc']:.3f} "
          f"f1+={summary['f1_pos']:.3f} f1-={summary['f1_neg']:.3f} gate={summary['gate_acc']} "
          f"route p50={summary['route_ms_p50']:.1f}ms p95={summary['route_ms_p95']:.1f}ms")
    for name, part in summary.get("split", {}).items():
        print(f"  {name}: n={part['n']} acc={part['acc']:.3f} "
              f"latency p50={part['latency_ms_p50']:.0f}ms p95={part['latency_ms_p95']:.0f}ms")
    if summary["failed"]:
        print("failed:", ", ".join(summary["failed"]))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        (out / "trajectories.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
