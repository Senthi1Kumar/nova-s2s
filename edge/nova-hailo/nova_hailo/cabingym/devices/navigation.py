# nova_hailo/cabingym/devices/navigation.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api,
    ApiContext,
    ApiResult,
    clarify,
    device,
    done,
    fmt_minutes,
    fmt_num,
    p_bool,
    p_enum,
    p_place,
    p_places,
    p_value,
)
from nova_hailo.cabingym.nav import Place, haversine_km

# What a driver asks to find on the way. Each maps to a Mapbox Search Box category (nav._CATEGORY_IDS); with
# only six kinds, "a car wash" became a charging station and "a pharmacy" a place to eat.
CATEGORIES = ("charging", "fuel", "parking", "coffee", "food", "fast_food", "restroom", "pharmacy", "car_wash",
              "car_repair", "grocery", "convenience", "atm", "bank", "post_office", "hospital", "doctor",
              "hotel", "bakery", "bar", "gym", "shopping", "park", "movie_theater")
CATEGORY_LABEL = {"charging": "charging station", "fuel": "gas station", "parking": "parking",
                  "coffee": "coffee place", "food": "place to eat", "fast_food": "fast food place",
                  "restroom": "restroom", "pharmacy": "pharmacy", "car_wash": "car wash",
                  "car_repair": "car repair shop", "grocery": "grocery store", "convenience": "convenience store",
                  "atm": "ATM", "bank": "bank", "post_office": "post office", "hospital": "hospital",
                  "doctor": "clinic", "hotel": "hotel", "bakery": "bakery", "bar": "bar", "gym": "gym",
                  "shopping": "shopping mall", "park": "park",
                  "movie_theater": "movie theater"}
CATEGORY_SYNONYMS = {
    "charging": ("charger", "chargers", "ev charger", "supercharger"),
    "fuel": ("gas", "petrol", "gas station"),
    "parking": ("parking lot", "garage", "car park"),
    "coffee": ("cafe", "café", "coffee shop"),
    "food": ("restaurant", "eat", "lunch", "dinner"),
    "fast_food": ("fast food", "burger", "drive through", "drive-through"),
    "restroom": ("bathroom", "toilet"),
    "pharmacy": ("chemist", "drugstore", "drug store", "medical store", "cvs", "walgreens"),
    "car_wash": ("car wash", "carwash", "car washing", "wash the car"),
    "car_repair": ("mechanic", "car repair", "auto repair", "tyre", "tire"),
    "grocery": ("supermarket", "groceries", "grocery store"),
    "convenience": ("convenience store", "7-eleven", "corner shop"),
    "atm": ("cash machine",),
    "post_office": ("post office", "usps"),
    "hospital": ("emergency room",),
    "doctor": ("clinic", "urgent care"),
    "hotel": ("motel", "lodging"),
    "gym": ("fitness",),
    "shopping": ("mall", "shopping center"),
    "movie_theater": ("theater", "theaters", "theatre", "theatres", "cinema", "cinemas", "movies",
                      "movie theater", "imax", "dolby atmos"),
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


def spoken_distance(km: float) -> str:
    """Distance as spoken; a place beside the car is "right by you"."""
    if km < 0.1:
        return "right by you"
    if km < 1.0:
        return f"about {round(km * 20) * 50:.0f} metres away"
    return f"{km:.1f} kilometres away"


def _spoken_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _navigate_to(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    dest = Place.from_dict(args["destination"])
    via = [Place.from_dict(v) for v in args.get("via") or ()]   # passed through in order
    route, targets = _route_targets(ctx, dest, via)
    through = f" via {_spoken_list([v.name for v in via])}" if via else ""
    skipped = args.get("_skipped_via") or []
    note = f" I can't route by {_spoken_list(skipped)} yet, so this is the fastest way." if skipped else ""
    return done(f"Navigating to {dest.name}{through}. About {fmt_minutes(route.eta_min)}.{note}", targets)


def _add_stop(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if not ctx.snapshot[c.NAV_ACTIVE]:
        return clarify("You're not navigating anywhere yet.")
    stop = Place.from_dict(args["destination"])
    dest = Place(ctx.snapshot[c.NAV_DEST], ctx.snapshot[c.NAV_DEST_LAT], ctx.snapshot[c.NAV_DEST_LON])
    stops = [Place.from_dict(s) for s in ctx.snapshot[c.NAV_STOPS]]
    if haversine_km(stop, dest) < 0.08:
        return clarify(f"{dest.name} is already where we're going.")
    if args.get("after"):   # "after Kirk's, a car wash": the new place is the new end, the old one a stop
        route, targets = _route_targets(ctx, stop, stops + [dest])
        return done(f"After {dest.name}, on to {stop.name}. You'll arrive in about "
                    f"{fmt_minutes(route.eta_min)}.", targets)
    route, targets = _route_targets(ctx, dest, stops + [stop])
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
    """Places of a kind near the car, near a named place, or along the road ahead,
    optionally within a distance or a driving time. Results become Nav.LastResults,
    so "add the second one as a stop" works next."""
    cat = args["category"]
    label = CATEGORY_LABEL[cat]
    here = _here(ctx)
    along = bool(args.get("along_route"))
    if along and len(ctx.route_ahead) < 2:
        return clarify(f"You're not on a route. Want the nearest {label} instead?")
    center = here
    where = ""
    if along:
        where = " on your route"
    elif args.get("near"):
        center = Place.from_dict(args["near"])
        where = f" near {center.name}"
    within_km = args.get("within_km")
    within_min = args.get("within_min")
    limit = 8 if within_km or within_min else 3
    found = ctx.nav.search(cat, center, category=cat, along_route=along,
                           route=ctx.route_ahead if along else (), limit=limit)
    if within_km:
        found = [p for p in found if haversine_km(here, p) <= float(within_km)]
    minutes: list[float | None] = []
    if within_min and found:
        minutes = ctx.nav.drive_minutes(here, found)
        kept = [(p, m) for p, m in zip(found, minutes) if m is not None and m <= float(within_min)]
        found, minutes = [p for p, _ in kept], [m for _, m in kept]
    results = found[:3]
    if not results:
        limit_text = (f" within {fmt_num(float(within_min))} minutes" if within_min else
                      f" within {fmt_num(float(within_km))} kilometres" if within_km else "")
        return done(f"I couldn't find a {label}{where or ' nearby'}{limit_text}.", {c.NAV_RESULTS: ()})
    first = results[0]
    away = (f"about {fmt_minutes(minutes[0])} away" if minutes else
            spoken_distance(haversine_km(here, first)))
    speak = f"The closest {label}{where} is {first.name}, {away}."
    if len(results) > 1:
        speak += " Also " + " and ".join(p.name for p in results[1:]) + "."
    return done(speak, {c.NAV_RESULTS: tuple(p.to_dict() for p in results)},
                data=[p.to_dict() for p in results])


_NAV_WRITES = (c.NAV_ACTIVE, c.NAV_DEST, c.NAV_ETA, c.NAV_DIST, c.NAV_DEST_LAT, c.NAV_DEST_LON,
               c.NAV_STOPS)
DEVICE = device(
    "navigation", "navigation: destinations, stops, arrival time and nearby places",
    r"\b(navigat\w*|directions|route|eta|arriv\w*|take\s+me|drive\s+to|stop|charg\w*|gas|fuel|"
    r"parking|coffee|nearby|nearest|closest|home|work)\b",
    [
        Api("stop_navigation", "end the route. Not for \"stop at <place>\": that adds a stop (add_stop)", _stop, writes=_NAV_WRITES,
            patterns=(r"\b(stop|cancel|end|quit|exit)\s+(the\s+)?(navigation|route|directions|"
                      r"guidance|trip)\b",)),
        Api("get_eta", "tell the arrival time", _eta, read_only=True,
            patterns=(r"\b(eta|arrival\s+time|when\s+(will|do)\s+(we|i)\s+(get|arrive)|"
                      r"how\s+(long|far)\s+(until|till|to\s+go|is\s+it|left)|time\s+to\s+destination)\b",)),
        Api("add_stop",
            "add a stop on the way, e.g. to pick someone up or drop them off at a place; with after, go "
            "there after the current destination instead",
            _add_stop,
            params={"destination": p_place("Where to stop"),
                    "after": p_bool("After the current destination: it becomes the new destination",
                                    parser="after", default=False)},
            required=("destination",),
            writes=_NAV_WRITES,
            patterns=(r"\b(add\s+(a\s+)?(new\s+)?stop|stop\s+(off\s+)?at|(swing|pull)\s+by)\b",
                      r"\bpick\s+up\s+(?!(the\s+)?(call|phone)\b)\w+.*\bat\b",
                      r"\bdrop\s+(\w+\s+){0,2}off\b.*\bat\b|\bdrop\s+off\b.*\bat\b")),
        Api("find_nearby",
            "find places such as a charger, gas, parking, coffee or a restaurant: near the car, "
            "near a named town, or along the current route; optionally within a distance or a "
            "driving time. Use this, not web search, to find a place to stop",
            _find_nearby,
            params={"category": p_enum(CATEGORIES, desc="Kind of place", synonyms=CATEGORY_SYNONYMS),
                    "along_route": p_bool("Search along the current route (on the way)",
                                          parser="along_route", default=False),
                    "near": p_place("A town or place to search around instead of the car"),
                    "within_km": p_value("within_km", "Only places within this many kilometres", 0.1, 300),
                    "within_min": p_value("within_min", "Only places within this many minutes' drive",
                                          1, 180)},
            required=("category",), writes=(c.NAV_RESULTS,), needs_live_nav=True,
            patterns=(r"^(?=.*\b(nearest|closest|nearby|near\s+(me|here|us)|around\s+here|"
                      r"on\s+(the|my|our)\s+way|along\s+the\s+route|within)\b)(?=.*\b(charg\w*|gas|petrol|fuel|"
                      r"parking|coffee|caf[eé]|restaurant|food|eat|restroom|bathroom|toilet|pharmacy|chemist|"
                      r"drug\s*store|car\s*wash\w*|mechanic|grocer\w*|supermarket|atm|bank|post\s+office|"
                      r"hospital|clinic|hotel|bakery|bar|gym|mall|theat\w*|cinemas?|movies?|imax)\b)",)),
        Api("navigate_to",
            "start navigation to a place; if speech misheard the name, pass the place "
            "the driver most likely means. \"First X, then Y\": destination Y with via [X]",
            _navigate_to,
            params={"destination": p_place("Where to go. Keep the town the driver gives: "
                                           "'Barbara Drive, Los Gatos', not 'Barbara Drive'"),
                    "via": p_places("Places or towns to drive through on the way, in order, when the "
                                    "driver says via or through: ['Los Altos', 'Cupertino']")},
            required=("destination",),
            writes=_NAV_WRITES,
            patterns=(r"\b(navigate|directions|take\s+me|drive\s+(me\s+)?to|head\s+to|go\s+to|"
                      r"route\s+(me\s+)?to|get\s+me\s+to)\b",)),
    ],
)
