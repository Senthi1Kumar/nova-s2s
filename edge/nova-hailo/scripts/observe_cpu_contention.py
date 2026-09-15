#!/usr/bin/env python3
"""Watch per-thread CPU on the running server, to see whether STT and TTS fight.

Thread pinning (OMP_NUM_THREADS) fixes a problem we have assumed rather than
observed: that Parakeet and Piper, which share one process and both default to
every core, collide at turn boundaries when TTS starts while STT is finishing.

The existing telemetry cannot answer this. `nova_hailo/bench/contracts.py` and
`nova_hailo/web/app.py` both report a whole-process CPU percentage, and a single
380% figure cannot distinguish "STT alone using four cores" from "STT and TTS
fighting over four cores" -- which imply opposite decisions. This samples
/proc/<pid>/task/*/stat and groups threads by role, so a live session shows
which one is actually happening.

Run it on the Pi while speaking to the demo:

    .venv/bin/python3 scripts/observe_cpu_contention.py --seconds 120

What to look for:
  * total CPU pinned near 400% on a 4-core Pi during a turn -> saturated
  * onnxruntime workers busy in the same sample as the TTS worker -> the
    contention pinning is meant to fix; the A/B is worth running
  * peaks well under 400%, or STT and TTS never busy together -> no contention,
    so revert OMP_NUM_THREADS to 4 rather than paying its median cost
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

CLK_TCK = 100.0  # _SC_CLK_TCK on Linux/aarch64; jiffies per second

# Thread names come from whoever spawned them. Anything unmatched lands in
# "other", which is itself informative: a large "other" means this map is stale,
# not that the process is idle.
ROLES = (
    ("tts", ("tts-worker", "pcm-cache-warm")),
    ("onnx", ("ort", "onnxruntime", "omp")),
    ("stt", ("parakeet", "nemo", "stt")),
    ("vad", ("vad", "silero", "dtln")),
    ("web", ("uvicorn", "asyncio", "anyio", "ws")),
)


def find_server_pid(port: int) -> int | None:
    """The process holding the web port."""
    try:
        out = subprocess.run(
            ["ss", "-lptnH", f"sport = :{port}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None
    # ss prints: users:(("python3",pid=1234,fd=7))
    for token in out.replace("(", " ").replace(")", " ").replace(",", " ").split():
        if token.startswith("pid="):
            try:
                return int(token[4:])
            except ValueError:
                continue
    return None


def role_of(comm: str) -> str:
    low = comm.lower()
    for role, needles in ROLES:
        if any(n in low for n in needles):
            return role
    return "other"


def sample(pid: int) -> dict[int, tuple[str, int]]:
    """tid -> (comm, cumulative jiffies). Threads may vanish mid-walk."""
    out: dict[int, tuple[str, int]] = {}
    task_dir = Path(f"/proc/{pid}/task")
    try:
        entries = list(task_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            raw = (entry / "stat").read_text()
        except OSError:
            continue  # thread exited between listing and reading
        # comm is parenthesised and may itself contain spaces or parens, so
        # split around the last ')' rather than trusting field positions.
        try:
            _, _, rest = raw.partition("(")
            comm, _, tail = rest.rpartition(")")
            fields = tail.split()
            utime, stime = int(fields[11]), int(fields[12])
            tid = int(entry.name)
        except (ValueError, IndexError):
            continue
        out[tid] = (comm, utime + stime)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--quiet-pct", type=float, default=25.0,
                    help="hide samples below this total CPU%%, so idle waiting "
                         "does not bury the turns")
    args = ap.parse_args()

    pid = args.pid or find_server_pid(args.port)
    if not pid or not Path(f"/proc/{pid}").exists():
        print(f"no server process found on port {args.port}; pass --pid",
              file=sys.stderr)
        return 2

    cores = len(list(Path("/proc/cpuinfo").read_text().split("processor"))) - 1 or 4
    print(f"watching pid {pid} for {args.seconds:.0f}s "
          f"({args.interval:.2f}s samples, {cores} cores). Speak to the demo now.")
    print(f"{'t':>6}  {'total':>7}  {'stt':>6} {'tts':>6} {'onnx':>6} "
          f"{'vad':>6} {'web':>6} {'other':>6}   thr")

    rows: list[str] = []
    prev = sample(pid)
    t0 = time.monotonic()
    peak = 0.0
    overlaps = 0
    samples = 0
    # Busiest raw thread names. A first run against this server classified every
    # thread as "other", which made the overlap count read as "no contention"
    # when it actually meant "the role map matched nothing". Reporting raw names
    # makes that failure visible instead of silent.
    by_comm: dict[str, float] = {}

    while time.monotonic() - t0 < args.seconds:
        time.sleep(args.interval)
        cur = sample(pid)
        if not cur:
            print("process gone")
            break
        elapsed = time.monotonic() - t0
        by_role: dict[str, float] = {}
        for tid, (comm, jiffies) in cur.items():
            before = prev.get(tid)
            delta = jiffies - before[1] if before else 0
            if delta <= 0:
                continue
            pct = (delta / CLK_TCK) / args.interval * 100.0
            role = role_of(comm)
            by_role[role] = by_role.get(role, 0.0) + pct
            by_comm[comm] = by_comm.get(comm, 0.0) + pct
        prev = cur
        total = sum(by_role.values())
        peak = max(peak, total)
        samples += 1
        # The specific thing pinning targets: STT and TTS burning CPU inside
        # the same sample window.
        busy_stt = by_role.get("stt", 0) + by_role.get("onnx", 0) > 20
        busy_tts = by_role.get("tts", 0) > 20
        if busy_stt and busy_tts:
            overlaps += 1

        if total >= args.quiet_pct:
            print(f"{elapsed:6.1f}  {total:6.0f}%  "
                  f"{by_role.get('stt', 0):5.0f}% {by_role.get('tts', 0):5.0f}% "
                  f"{by_role.get('onnx', 0):5.0f}% {by_role.get('vad', 0):5.0f}% "
                  f"{by_role.get('web', 0):5.0f}% {by_role.get('other', 0):5.0f}%"
                  f"   {len(cur)}"
                  f"{'  <-- STT+TTS OVERLAP' if busy_stt and busy_tts else ''}",
                  flush=True)
        if args.csv:
            for role, pct in by_role.items():
                rows.append(f"{elapsed:.2f},{role},{pct:.1f}")

    ceiling = cores * 100
    print(f"\npeak total CPU: {peak:.0f}% of {ceiling}% ({cores} cores)")

    print("busiest threads (cumulative CPU%):")
    ranked = sorted(by_comm.items(), key=lambda kv: -kv[1])[:10]
    for comm, pct_sum in ranked:
        print(f"  {comm:<20} {pct_sum:>9.0f}  -> role={role_of(comm)}")
    unmapped = sum(v for c, v in by_comm.items() if role_of(c) == "other")
    total_cpu = sum(by_comm.values()) or 1.0
    if unmapped / total_cpu > 0.5:
        print(f"\n  WARNING: {unmapped / total_cpu:.0%} of CPU landed in 'other'.")
        print("  The role map above does not match this build's thread names, so")
        print("  the overlap count below is NOT evidence of anything. Add the")
        print("  names listed here to ROLES and re-run before drawing conclusions.")
        print(f"  (the peak figure, {peak:.0f}%, is still valid -- it is a sum)")

    print(f"\nSTT/TTS overlap samples: {overlaps}/{samples}")
    if peak < ceiling * 0.75:
        print("VERDICT: never saturated the box -- pinning is solving a problem "
              "that is not happening. Revert OMP_NUM_THREADS to 4.")
    elif overlaps == 0:
        print("VERDICT: saturated, but STT and TTS never overlapped -- the "
              "contention is with something else. Pinning them is the wrong fix.")
    else:
        print("VERDICT: real STT/TTS contention observed -- the A/B on "
              "OMP_NUM_THREADS 2 vs 4 is worth running.")

    if args.csv and rows:
        args.csv.write_text("t_s,thread,cpu_pct\n" + "\n".join(rows) + "\n")
        print(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
