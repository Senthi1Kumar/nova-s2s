#!/usr/bin/env python3
"""Run the s2s-native eval corpus through lexical route (+ optional mock agent).

Never imports ``nova.engine`` or LiteRT. Gold labels must match live atomic
tool names from ``nova.server.tool_service.build_registry``.

Examples:
  uv run python eval/run_corpus.py
  uv run python eval/run_corpus.py --mode mock-agent
  uv run python eval/run_corpus.py --auth --json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Repo root on sys.path when invoked as ``python eval/run_corpus.py``.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval import CORPUS_VERSION  # noqa: E402
from eval.scorers import (  # noqa: E402
    aggregate_no_tool_pr,
    latency_percentiles,
    normalize_auth_status,
    score_turn,
)

DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "s2s_turns.jsonl"

# Live registry tool names (must stay in sync with tool_service.build_registry).
KNOWN_TOOLS: frozenset[str] = frozenset(
    {
        "set_hvac",
        "set_sunroof",
        "set_windows",
        "query_vehicle_status",
        "set_reminder",
        "list_reminders",
        "query_calendar",
        "web_search",
        "get_weather",
        "research_topic",
        "check_email",
        "check_calendar",
        "create_calendar_event",
        "delete_calendar_event",
        "list_drive_files",
        "create_drive_folder",
        "play_music",
        "start_research",
        "get_research_result",
        "remember",
        "recall_memories",
        "send_email",
        "send_payment",
    }
)


def load_fixtures(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict) or "id" not in row or "transcript" not in row:
            raise SystemExit(f"{path}:{line_no}: missing id/transcript")
        rows.append(row)
    return rows


def _forced_name(route: dict[str, Any]) -> str | None:
    """Map route_turn output to a single predicted tool (or None = no tool)."""
    choice = route.get("tool_choice")
    if choice == "none":
        return None
    if isinstance(choice, dict) and choice.get("type") == "function":
        name = choice.get("name")
        return str(name) if name else None
    top = route.get("top_name")
    return str(top) if top else None


def _build_registry(tmp: Path):
    """Minimal live registry — same names as tool_service, temp DBs only."""
    from nova.server.tool_service import build_registry
    from nova.tools.vehicle import VehicleDB

    return build_registry(
        db=VehicleDB(tmp / "vehicle.db"),
        driveauth_store=tmp / "driveauth",
    )


def predict_route(
    registry: dict[str, Any],
    transcript: str,
    *,
    pinned: set[str] | None = None,
) -> dict[str, Any]:
    from nova.server.routing import route_turn

    t0 = time.perf_counter()
    route = route_turn(registry, transcript, k=8, pinned=pinned)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    name = _forced_name(route)
    return {
        "tool": name,
        "args": {},
        "no_tool": name is None,
        "route": route,
        "latency_ms": elapsed_ms,
        "auth_status": None,
        "calls": ([{"name": name, "args": {}}] if name else []),
        "speak": None,
        "tool_payload": None,
    }


def predict_mock_agent(fixture: dict[str, Any]) -> dict[str, Any]:
    """Oracle agent: emit gold labels (scorer smoke / upper bound)."""
    expect = fixture.get("expect") or {}
    tool = expect.get("tool")
    no_tool = bool(expect.get("no_tool") or expect.get("unsupported") or tool is None)
    if "no_tool" in expect:
        no_tool = bool(expect["no_tool"])
    if expect.get("unsupported") and expect.get("no_tool", True):
        no_tool = True
    args = dict(expect.get("args") or {})
    auth = expect.get("auth_status")
    speak = None
    payload = None
    if tool == "send_payment" and "amount" in args:
        payload = {"amount": args["amount"], "payee": args.get("payee", "")}
        speak = f"Paid {args['amount']} INR to {args.get('payee', '')}."
    return {
        "tool": None if no_tool else tool,
        "args": {} if no_tool else args,
        "no_tool": no_tool,
        "route": None,
        "latency_ms": 0.0,
        "auth_status": auth,
        "calls": ([] if no_tool or not tool else [{"name": tool, "args": args}]),
        "speak": speak,
        "tool_payload": payload,
    }


def _maybe_auth(transcript: str, session_id: str) -> str | None:
    from nova.server.driveauth_bridge import precheck

    out = precheck(session_id=session_id, transcript=transcript)
    status = out.get("status")
    if status == "bypass":
        return "bypass"
    if status == "step_up_required":
        return "step_up"
    if status == "denied":
        return "reject"
    return status


def run_corpus(
    fixtures: list[dict[str, Any]],
    *,
    mode: str = "route",
    with_auth: bool = False,
    tag_filter: set[str] | None = None,
) -> dict[str, Any]:
    tag_filter = tag_filter or set()
    results: list[dict[str, Any]] = []
    no_tool_counts: list[dict[str, int]] = []
    latencies: list[float] = []
    tool_hits = tool_total = 0
    args_hits = args_total = 0
    auth_hits = auth_total = 0
    malformed_n = markup_n = dup_n = 0
    skipped = 0

    with tempfile.TemporaryDirectory(prefix="nova_eval_") as tmp_s:
        tmp = Path(tmp_s)
        registry = _build_registry(tmp)
        known = set(registry.keys()) | set(KNOWN_TOOLS)

        if with_auth:
            import os

            from nova.server.driveauth_bridge import reset_auth_for_tests

            os.environ["DRIVEAUTH_USE_MOCK"] = "1"
            os.environ["DRIVEAUTH_SEED_MATURE"] = "1"
            os.environ["DRIVEAUTH_STORE_DIR"] = str(tmp / "driveauth_auth")
            os.environ["DRIVEAUTH_DRIVER_ID"] = "driver1"
            reset_auth_for_tests()

        for fix in fixtures:
            tags = set(fix.get("tags") or [])
            if tag_filter and not (tags & tag_filter):
                skipped += 1
                continue
            # Route path only scores force/top_name — skip agent-arg / context turns.
            if mode == "route" and (tags & {"agent_only", "agent_args"}):
                skipped += 1
                continue

            transcript = fix["transcript"]
            expect = fix.get("expect") or {}

            if mode == "mock-agent":
                pred = predict_mock_agent(fix)
            else:
                pinned = set()
                if "pinned" in tags and expect.get("tool"):
                    pinned.add(str(expect["tool"]))
                pred = predict_route(registry, transcript, pinned=pinned or None)
                if with_auth and expect.get("auth_status") is not None:
                    pred["auth_status"] = _maybe_auth(transcript, f"eval-{fix['id']}")
                if "route_soft" in tags and expect.get("unsupported"):
                    choice = (pred.get("route") or {}).get("tool_choice")
                    top = pred.get("tool")
                    soft_ok = choice == "required" and top in {
                        "list_reminders",
                        "set_reminder",
                        None,
                    }
                    if soft_ok or top in {"list_reminders", "set_reminder"}:
                        pred["tool"] = None
                        pred["no_tool"] = True

            scored = score_turn(
                expect=expect,
                predicted_tool=pred["tool"],
                predicted_args=pred.get("args"),
                predicted_no_tool=pred.get("no_tool"),
                predicted_auth=pred.get("auth_status"),
                speak=pred.get("speak"),
                tool_payload=pred.get("tool_payload"),
                calls=pred.get("calls"),
                known_tools=known,
            )
            no_tool_counts.append(scored["no_tool"])
            latencies.append(float(pred.get("latency_ms") or 0.0))

            tool_total += 1
            if scored["tool_ok"]:
                tool_hits += 1

            if scored.get("args") is not None:
                args_total += 1
                if scored["args"]["ok"]:
                    args_hits += 1
            if scored.get("auth_ok") is not None:
                auth_total += 1
                if scored["auth_ok"]:
                    auth_hits += 1
            if scored["malformed"]:
                malformed_n += 1
            if scored["forbidden_markup"]:
                markup_n += 1
            if not scored["duplicate"]["ok"]:
                dup_n += 1

            results.append(
                {
                    "id": fix["id"],
                    "tags": list(tags),
                    "predicted_tool": pred["tool"],
                    "expected_tool": expect.get("tool"),
                    "tool_ok": scored["tool_ok"],
                    "args_ok": None if scored["args"] is None else scored["args"]["ok"],
                    "auth_ok": scored["auth_ok"],
                    "auth_pred": normalize_auth_status(pred.get("auth_status")),
                    "latency_ms": round(float(pred.get("latency_ms") or 0.0), 3),
                }
            )

    return {
        "corpus_version": CORPUS_VERSION,
        "mode": mode,
        "with_auth": with_auth,
        "n_fixtures": len(fixtures),
        "n_scored": len(results),
        "n_skipped": skipped,
        "tool_name_accuracy": round(tool_hits / tool_total, 4) if tool_total else None,
        "argument_slot_accuracy": round(args_hits / args_total, 4) if args_total else None,
        "auth_accuracy": round(auth_hits / auth_total, 4) if auth_total else None,
        "no_tool": aggregate_no_tool_pr(no_tool_counts),
        "malformed_calls": malformed_n,
        "forbidden_markup": markup_n,
        "duplicate_executions": dup_n,
        "latency_ms": latency_percentiles(latencies),
        "known_tools": sorted(KNOWN_TOOLS),
        "failures": [r for r in results if not r["tool_ok"]],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Run Nova s2s-native eval corpus (lexical route / mock agent)."
    )
    p.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help=f"JSONL fixtures path (default: {DEFAULT_FIXTURES})",
    )
    p.add_argument(
        "--mode",
        choices=("route", "mock-agent", "both"),
        default="route",
        help="route=lexical force path; mock-agent=gold oracle; both=print both",
    )
    p.add_argument(
        "--auth",
        action="store_true",
        help="Also run DriveAuth precheck for fixtures with auth_status (mock store).",
    )
    p.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Only score fixtures carrying this tag (repeatable).",
    )
    p.add_argument("--json", action="store_true", help="Print summary JSON only.")
    args = p.parse_args(argv)

    fixtures = load_fixtures(args.fixtures)
    tag_filter = set(args.tag) if args.tag else None
    modes = ["route", "mock-agent"] if args.mode == "both" else [args.mode]
    out: dict[str, Any] = {"corpus_version": CORPUS_VERSION, "runs": {}}
    for mode in modes:
        out["runs"][mode] = run_corpus(
            fixtures, mode=mode, with_auth=args.auth, tag_filter=tag_filter
        )

    payload = out["runs"][modes[0]] if len(modes) == 1 else out
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not args.json and len(modes) == 1:
        s = payload
        print(
            f"\n# tool_acc={s.get('tool_name_accuracy')} "
            f"args_acc={s.get('argument_slot_accuracy')} "
            f"no_tool_f1={s['no_tool'].get('f1')} "
            f"scored={s['n_scored']} skipped={s['n_skipped']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
