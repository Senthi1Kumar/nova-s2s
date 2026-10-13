# nova_hailo/cabingym/devices/body.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clamp, device, done, p_bool, p_enum, p_value,
)

WIPER_SPEAK = {"off": "Wipers off.", "auto": "Wipers on auto.", "low": "Wipers on.",
               "high": "Wipers on fast."}


def _sunroof(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    v = int(clamp(float(args["value"]), -100, 100))
    speak = {0: "Sunroof closed.", 100: "Sunroof open.", -100: "Sunroof tilted."}.get(
        v, f"Sunroof opened to {v} percent.")
    return done(speak, {c.SUNROOF: v})


def _sunshade(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    v = int(clamp(float(args["value"]), 0, 100))
    speak = {100: "Sunshade closed.", 0: "Sunshade open."}.get(v, f"Sunshade at {v} percent.")
    return done(speak, {c.SUNSHADE: v})


DEVICE = device(
    "body", "sunroof, roof shade, wipers and mirrors",
    r"\b(sun\s*roof|moon\s*roof|roof|sun\s*shade|wipers?|mirrors?)\b",
    [
        Api("set_sunshade", "open or close the roof sunshade", _sunshade,
            params={"value": p_value("shade", "0 open, 100 closed", 0, 100)}, required=("value",),
            writes=(c.SUNSHADE,),
            patterns=(r"\b(sun\s*shade|roof\s+(shade|blind)|sunroof\s+(shade|blind))\b",)),
        Api("set_sunroof", "open, close or tilt the sunroof", _sunroof,
            params={"value": p_value("opening", "0 closed, 100 open, -100 tilted", -100, 100)},
            required=("value",), writes=(c.SUNROOF,),
            patterns=(r"\b(sun\s*roof|moon\s*roof|panoramic\s+roof|roof)\b",)),
        Api("set_wipers", "set the windshield wipers", lambda ctx, args: done(
                WIPER_SPEAK[args["mode"]], {c.WIPERS: args["mode"]}),
            params={"mode": p_enum(("off", "auto", "low", "high"), desc="Wiper mode",
                                   synonyms={"off": ("stop",),
                                             "auto": ("automatic", "rain sensor", "rain sensing"),
                                             "low": ("slow",), "high": ("fast", "max")},
                                   bool_map={"true": "low", "false": "off"})},
            required=("mode",), writes=(c.WIPERS,), patterns=(r"\bwipers?\b",)),
        Api("fold_mirrors", "fold or unfold the side mirrors", lambda ctx, args: done(
                "Mirrors folded." if args.get("folded", True) else "Mirrors unfolded.",
                {c.MIRRORS_FOLDED: bool(args.get("folded", True))}),
            params={"folded": p_bool("True to fold", parser="fold", default=True)},
            writes=(c.MIRRORS_FOLDED,), patterns=(r"\bmirrors?\b",)),
    ],
)
