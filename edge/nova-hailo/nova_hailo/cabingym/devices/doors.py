# nova_hailo/cabingym/devices/doors.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import Api, ApiContext, ApiResult, device, done, p_bool

LOCKS = tuple(c.door(side, "IsLocked") for side in c.DOOR_SIDES)
CHILD_LOCKS = tuple(c.door(side, "IsChildLockActive") for side in c.WINDOW_SIDES["rear"])


def _lock(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    locked = bool(args["locked"])
    return done("Doors locked." if locked else "Doors unlocked.", dict.fromkeys(LOCKS, locked))


def _child(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    on = bool(args.get("on", True))
    return done(f"Child locks {'on' if on else 'off'}.", dict.fromkeys(CHILD_LOCKS, on))


_TRUNK = r"(trunk|boot|tailgate|liftgate|hatch)"
DEVICE = device(
    "doors", "door locks, trunk and child locks", r"\b(doors?|lock\w*|unlock\w*|trunk|boot|tailgate|child)\b",
    [
        Api("set_child_lock", "turn the rear child locks on or off", _child,
            params={"on": p_bool("Child locks on", default=True)}, writes=CHILD_LOCKS,
            patterns=(r"\bchild\s*(safety\s+)?locks?\b",)),
        Api("open_trunk", "open the trunk",
            lambda ctx, args: done("Opening the trunk.", {c.TRUNK: True}), writes=(c.TRUNK,),
            patterns=(rf"\b(open|pop)\s+(the\s+)?{_TRUNK}\b",)),
        Api("close_trunk", "close the trunk",
            lambda ctx, args: done("Closing the trunk.", {c.TRUNK: False}), writes=(c.TRUNK,),
            patterns=(rf"\b(close|shut)\s+(the\s+)?{_TRUNK}\b",)),
        Api("lock_doors", "lock or unlock the doors", _lock,
            params={"locked": p_bool("True to lock, false to unlock", parser="lock")},
            required=("locked",), writes=LOCKS, patterns=(r"\b(un)?lock\b",)),
    ],
)
