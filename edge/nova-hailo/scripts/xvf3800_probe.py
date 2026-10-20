#!/usr/bin/env python3
"""Print the reSpeaker XVF3800's live status: speech energy per beam after its echo canceller,
whether the canceller has converged, and the speaker's direction.

  .venv/bin/python scripts/xvf3800_probe.py            # 10 lines a second until Ctrl+C
  .venv/bin/python scripts/xvf3800_probe.py --seconds 20

Speak, stay quiet, and let Nova talk: the auto beam's energy should rise for your voice and
stay near 0 for Nova's. "board status unavailable" means pyusb or the udev rule is missing
(scripts/setup_pi.sh adds both).
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nova_hailo.xvf3800 import Xvf3800  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long (0 = until Ctrl+C)")
    ap.add_argument("--hz", type=float, default=10.0)
    a = ap.parse_args()
    reader = Xvf3800(poll_hz=a.hz)
    if not reader.open():
        print(f"board status unavailable: {reader.error}", file=sys.stderr)
        return 1
    print(f"XVF3800 firmware {reader.version}. Columns: auto-beam energy, beams 1/2/free, converged, "
          f"path change, direction (degrees)")
    end = time.monotonic() + a.seconds if a.seconds > 0 else math.inf
    try:
        while time.monotonic() < end:
            s = reader.poll_once()
            if s is not None:
                deg = f"{math.degrees(s.azimuth):6.0f}" if s.azimuth is not None else "     -"
                bar = "#" * min(40, int(math.log10(s.speech_energy + 1) * 5))
                print(f"{s.speech_energy:12.0f} {s.energy[0]:10.0f} {s.energy[1]:10.0f} {s.energy[2]:10.0f}  "
                      f"conv={int(s.converged)} path={int(s.path_change)} dir={deg}  {bar}", flush=True)
            time.sleep(1.0 / a.hz)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
