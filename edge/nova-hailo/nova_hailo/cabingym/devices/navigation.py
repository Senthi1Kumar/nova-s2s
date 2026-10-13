# nova_hailo/cabingym/devices/navigation.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clarify, device, done, fmt_minutes, p_enum, p_place,
)
from nova_hailo.cabingym.nav import Place, haversine_km

CATEGORIES = ("charging", "fuel", "parking", "coffee", "food", "restroom")
CATEGORY_LABEL = {"charging": "charging station", "fuel": "gas station", "parking": "parking",
                  "coffee": "coffee place", "food": "place to eat", "restroom": "restroom"}
CATEGORY_SYNONYMS = {
    "charging": ("charger", "chargers", "ev charger", "supercharger"),
    "fuel": ("gas", "petrol", "gas station"),
    "parking": ("park", "parking lot", "garage"),
    "coffee": ("cafe", "café"),
    "food": ("restaurant", "eat", "lunch", "dinner"),
    "restroom": ("bathroom", "toilet"),
}


def _here(ctx: ApiContext) -> Place:
    return Place("here", ctx.snapshot[c.LAT], ctx.snapshot[c.LON])


def _route_targets(ctx: ApiContext, dest: Place, stops: list[Place]):
    route = ctx.nav.route(_here(ctx), [*stops, dest])
    targets = {
        c.NAV_ACTIVE: True, c.NAV_DEST: dest.name, c.NAV_ETA: route.eta_min,
        c.NAV_DIST: route.distance_km, c.NAV_DEST_LAT: dest.lat, c.NAV_DEST_LON: dest.lon,
        c.NAV_STOPS: tuple(s.to_dict() for s in stops),
    }
    return route, targets


def _navigate_to(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    dest = Place.from_dict(args["destination"])
    route, targets = _route_targets(ctx, dest, [])
    return done(f"Navigating to {dest.name}. About {fmt_minutes(route.eta_min)}.", targets)


def _add_stop(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if not ctx.snapshot[c.NAV_ACTIVE]:
        return clarify("You're not navigating anywhere yet.")
    stop = Place.from_dict(args["destination"])
    stops = [Place.from_dict(s) for s in ctx.snapshot[c.NAV_STOPS]] + [stop]
    dest = Place(ctx.snapshot[c.NAV_DEST], ctx.snapshot[c.NAV_DEST_LAT], ctx.snapshot[c.NAV_DEST_LON])
    route, targets = _route_targets(ctx, dest, stops)
    return done(f"Added a stop at {stop.name}. You'll arrive in about {fmt_minutes(route.eta_min)}.",
                targets)


def _stop(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if not ctx.snapshot[c.NAV_ACTIVE]:
        return done("You're not navigating right now.")
    return done("Navigation stopped.", {c.NAV_ACTIVE: False, c.NAV_DEST: "", c.NAV_ETA: 0.0,
                                        c.NAV_DIST: 0.0, c.NAV_STOPS: ()})


def _eta(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    snap = ctx.snapshot
    if not snap[c.NAV_ACTIVE]:
        return done("You're not navigating right now.")
    return done(f"About {fmt_minutes(snap[c.NAV_ETA])} to {snap[c.NAV_DEST]}, "
                f"{snap[c.NAV_DIST]:.1f} kilometres.")


def _find_nearby(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    cat = args["category"]
    here = _here(ctx)
    results = ctx.nav.search(cat, here, category=cat)[:3]
    if not results:
        return done(f"I couldn't find a {CATEGORY_LABEL[cat]} nearby.", {c.NAV_RESULTS: ()})
    first = results[0]
    return done(
        f"The closest {CATEGORY_LABEL[cat]} is {first.name}, "
        f"{haversine_km(here, first):.1f} kilometres away.",
        {c.NAV_RESULTS: tuple(p.to_dict() for p in results)},
        data=[p.to_dict() for p in results],
    )


_NAV_WRITES = (c.NAV_ACTIVE, c.NAV_DEST, c.NAV_ETA, c.NAV_DIST, c.NAV_DEST_LAT, c.NAV_DEST_LON,
               c.NAV_STOPS)
DEVICE = device(
    "navigation", "navigation: destinations, stops, arrival time and nearby places",
    r"\b(navigat\w*|directions|route|eta|arriv\w*|take\s+me|drive\s+to|stop|charg\w*|gas|fuel|"
    r"parking|coffee|nearby|nearest|closest|home|work)\b",
    [
        Api("stop_navigation", "stop navigating", _stop, writes=_NAV_WRITES,
            patterns=(r"\b(stop|cancel|end|quit|exit)\s+(the\s+)?(navigation|route|directions|"
                      r"guidance|trip)\b",)),
        Api("get_eta", "tell the arrival time", _eta, read_only=True,
            patterns=(r"\b(eta|arrival\s+time|when\s+(will|do)\s+(we|i)\s+(get|arrive)|"
                      r"how\s+(long|far)\s+(until|till|to\s+go|is\s+it|left)|time\s+to\s+destination)\b",)),
        Api("add_stop",
            "add a stop on the way, e.g. to pick someone up or drop them off at a place",
            _add_stop,
            params={"destination": p_place("Where to stop")}, required=("destination",),
            writes=_NAV_WRITES,
            patterns=(r"\b(add\s+(a\s+)?(new\s+)?stop|stop\s+(off\s+)?at|(swing|pull)\s+by)\b",
                      r"\bpick\s+up\s+(?!(the\s+)?(call|phone)\b)\w+.*\bat\b",
                      r"\bdrop\s+(\w+\s+){0,2}off\b.*\bat\b|\bdrop\s+off\b.*\bat\b")),
        Api("find_nearby", "find a nearby place such as a charger, gas, parking or coffee",
            _find_nearby,
            params={"category": p_enum(CATEGORIES, desc="Kind of place", synonyms=CATEGORY_SYNONYMS)},
            required=("category",), writes=(c.NAV_RESULTS,), needs_live_nav=True,
            patterns=(r"^(?=.*\b(nearest|closest|nearby|near\s+(me|here|us)|around\s+here|"
                      r"on\s+(the|my)\s+way|along\s+the\s+route)\b)(?=.*\b(charg\w*|gas|petrol|fuel|"
                      r"parking|coffee|caf[eé]|restaurant|food|eat|restroom|bathroom|toilet)\b)",)),
        Api("navigate_to",
            "start navigation to a place; if speech misheard the name, pass the place "
            "the driver most likely means",
            _navigate_to,
            params={"destination": p_place("Where to go")}, required=("destination",),
            writes=_NAV_WRITES,
            patterns=(r"\b(navigate|directions|take\s+me|drive\s+(me\s+)?to|head\s+to|go\s+to|"
                      r"route\s+(me\s+)?to|get\s+me\s+to)\b",)),
    ],
)
