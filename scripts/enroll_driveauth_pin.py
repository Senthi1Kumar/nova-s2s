"""Enroll (or replace) the driver's local payment PIN for DriveAuth step-up.

Usage: uv run python scripts/enroll_driveauth_pin.py 4321 [--driver driver1]
"""
import argparse
from pathlib import Path

from driveauth.step_up_fallback import enroll_pin


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pin", help="Numeric PIN, min 4 digits")
    p.add_argument("--driver", default="driver1")
    p.add_argument("--store", default=str(Path("runtime") / "driveauth_store"))
    args = p.parse_args()
    ok = enroll_pin(args.store, args.driver, args.pin)
    print("PIN enrolled." if ok else "FAILED — PIN too short or store not writable.")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
