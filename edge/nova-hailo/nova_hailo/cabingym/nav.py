# nova_hailo/cabingym/nav.py
"""Navigation provider seam.

The navigation device never computes geography itself; it hands structured
requests to a NavProvider, the way production assistants hand them to the
OEM nav service. FixtureNav is deterministic and offline. MapboxNav searches
live places with the Search Box API and leaves the drawn road line to NavFeed.
"""
from __future__ import annotations

import difflib
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# Search Box text search returns coordinates in one call. Geocoding v6 has no POI type.
_API_BASE = "https://api.mapbox.com"
_SEARCH_BASE = _API_BASE + "/search/searchbox/v1"
_MATRIX_PATH = "/directions-matrix/v1/mapbox/driving-traffic"
_MATRIX_MAX = 10   # coordinates per driving-traffic matrix request
_FORWARD_TYPES = "poi,place,locality,address,street,neighborhood,city"
# Canonical ids from Search Box /list/category. Not a list of destinations.
_CATEGORY_IDS = {
    "charging": "charging_station",
    "fuel": "gas_station",
    "parking": "parking_lot",
    "coffee": "coffee",
    "food": "food_and_drink",
    "fast_food": "fast_food",
    "pharmacy": "pharmacy",
    "car_wash": "car_wash",
    "car_repair": "auto_repair",
    "grocery": "grocery",
    "convenience": "convenience_store",
    "atm": "atm",
    "bank": "bank",
    "post_office": "post_office",
    "hospital": "hospital",
    "doctor": "medical_clinic",
    "hotel": "hotel",
    "bakery": "bakery",
    "bar": "bar",
    "gym": "fitness_center",
    "shopping": "shopping_mall",
    "park": "park",
    "airport": "airport",
    "movie_theater": "cinema",
}
# Words for a text search when a category search finds nothing.
_CATEGORY_TEXT = {"movie_theater": "movie theater", "car_repair": "auto repair", "doctor": "medical clinic"}
_LOCAL_SCORE = 0.68
_GLOBAL_SCORE = 0.84
_WIDE_KM = 80.0
_TIGHT_KM = 12.0
_AIRPORT_WORDS = frozenset({"airport", "airports", "airfield"})
_AIRPORT_FILL = frozenset({
    "the", "a", "an", "nearby", "nearest", "closest", "near", "me", "here",
    "around", "local", "to", "my", "our",
})
_CORRECTION_REJECT = ("not sure", "don't know", "do not know", "dont know", "unknown")
# A trailing word that is a kind of place, not part of the proper name.
_TAIL_WORDS = frozenset({"park", "parks", "plaza", "mall", "center", "centre", "centers", "centres"})
# Words that say what kind of place it is, not which one: "the OpenAI office" is the place called OpenAI.
_KIND_WORDS = frozenset({"office", "offices", "hq", "headquarters", "building", "campus"})
_ARTICLES = frozenset({"the", "a", "an"})
# Kinds of venue. A query that ends in one ("Palo Alto Art Center") is not a different kind that shares the rest
# of the name ("Palo Alto Art Studio"), unless the names are near enough to be the same place.
_VENUE_KINDS = _TAIL_WORDS | _KIND_WORDS | frozenset({
    "studio", "studios", "school", "store", "shop", "clinic", "hospital", "hotel", "restaurant", "cafe", "bar",
    "gym", "library", "museum", "church", "station", "garage", "lab", "bank", "market", "theater", "theatre",
    "club", "salon", "academy", "college", "university", "apartments", "bakery", "pharmacy"})
# A hit below _EXACT_SCORE must also contain the query's identifying word at least this closely: shared letters
# from "office" and "Palo Alto" made "Immigration Law Offices Palo Alto" pass for "OpenAI office Palo Alto".
_KEY_WORD_MATCH = 0.85
# A wide hit this close to the heard name is the place. Weaker wide hits wait for the local pool.
_EXACT_SCORE = 0.92


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "lat": self.lat, "lon": self.lon}
        if self.category:
            out["category"] = self.category
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Place:
        return cls(str(d["name"]), float(d["lat"]), float(d["lon"]), d.get("category"))


@dataclass(frozen=True)
class Route:
    distance_km: float
    eta_min: float
    stops: tuple[Place, ...]


LatLon = tuple[float, float]


class NavProvider(Protocol):
    is_live: bool

    def search(
        self, query: str, near: Place, category: str | None = None, along_route: bool = False,
        *, route: tuple[LatLon, ...] = (), limit: int | None = None,
    ) -> list[Place]: ...

    def route(self, origin: Place, stops: list[Place]) -> Route: ...

    def drive_minutes(self, origin: Place, places: list[Place]) -> list[float | None]: ...


# A place this far from the road ahead is not "on the way" (offline fixture only;
# Mapbox measures the real detour with time_deviation).
_ON_ROUTE_KM = 1.5
# Minutes of detour Search Box allows for a place found along the route.
ROUTE_DETOUR_MIN = 10


def haversine_km(a: Place, b: Place) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


DEFAULT_POIS: tuple[Place, ...] = (
    Place("Hamilton Ave Chargers", 37.4455, -122.1612, "charging"),
    Place("University Circle Chargers", 37.4270, -122.1660, "charging"),
    Place("El Camino Fuel", 37.4290, -122.1400, "fuel"),
    Place("Page Mill Fuel", 37.4205, -122.1330, "fuel"),
    Place("Hilltop Cafe", 37.4430, -122.1580, "coffee"),
    Place("Blue Door Coffee", 37.4447, -122.1610, "coffee"),
    Place("Cardinal Parking Garage", 37.4460, -122.1590, "parking"),
    Place("Corner Taqueria", 37.4436, -122.1627, "food"),
    Place("Market Hall", 37.4390, -122.1580, "food"),
    Place("Town Square Restrooms", 37.4440, -122.1600, "restroom"),
)
_ROAD_FACTOR = 1.3
_STOPWORDS = frozenset({"the", "a", "an", "nearest", "closest", "nearby", "near", "me", "shop",
                        "place", "station", "some", "any", "to", "at", "of"})


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (text or "").lower()))


class FixtureNav:
    """Offline POIs; routes are straight-line distance times a road factor."""

    is_live = False

    def __init__(self, pois: tuple[Place, ...] = DEFAULT_POIS, speed_kmh: float = 40.0):
        self.pois = pois
        self.speed_kmh = speed_kmh

    def search(
        self, query: str, near: Place, category: str | None = None, along_route: bool = False,
        *, route: tuple[LatLon, ...] = (), limit: int | None = None,
    ) -> list[Place]:
        if category:
            hits = [p for p in self.pois if p.category == category]
            if route:
                hits = [p for p in hits
                        if min(haversine_km(p, Place("", a, b)) for a, b in route) <= _ON_ROUTE_KM]
            return sorted(hits, key=lambda p: haversine_km(near, p))
        words = _words(query) - _STOPWORDS

        def matched(p: Place) -> int:
            return len(words & (_words(p.name) | {p.category or ""}))

        # The place named outright ("Blue Door Coffee") before the nearest of its kind.
        hits = [p for p in self.pois if matched(p)]
        return sorted(hits, key=lambda p: (-matched(p), haversine_km(near, p)))

    def drive_minutes(self, origin: Place, places: list[Place]) -> list[float | None]:
        return [round(haversine_km(origin, p) * _ROAD_FACTOR / self.speed_kmh * 60.0, 1) for p in places]

    def route(self, origin: Place, stops: list[Place]) -> Route:
        dist = 0.0
        prev = origin
        for stop in stops:
            dist += haversine_km(prev, stop)
            prev = stop
        dist *= _ROAD_FACTOR
        return Route(round(dist, 1), round(dist / self.speed_kmh * 60.0, 1), tuple(stops))


def mapbox_token() -> str:
    """Mapbox token from the environment. Empty when place search must stay offline."""
    return (os.environ.get("MAPBOX_API_KEY") or os.environ.get("MAPBOX_ACCESS_TOKEN") or "").strip()


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def name_score(query: str, candidate: str) -> float:
    """How much of the heard name survives in a Mapbox result name. 1.0 is the same name.

    A branch suffix ("... Cinema Mountain View") or a spoken article ("the ...") does not
    make a name a worse match: the candidate's leading words are tried too.
    """
    heard = re.sub(r"^\s*(the|a|an)\s+", "", query or "", flags=re.I)
    left, right = _alnum(heard), _alnum(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    best = difflib.SequenceMatcher(None, left, right).ratio()
    words = (candidate or "").split()
    said = heard.split()
    # Only the same name with a branch on the end: the first word must already match.
    if not said or not words or difflib.SequenceMatcher(
            None, _alnum(said[0]), _alnum(words[0])).ratio() < 0.8:
        return best
    for k in range(max(1, len(said)), len(words)):
        head = _alnum(" ".join(words[:k]))
        if head:
            best = max(best, difflib.SequenceMatcher(None, left, head).ratio())
    return best


def _bbox(near: Place, km: float) -> str:
    """minLon,minLat,maxLon,maxLat around ``near``. Search Box rejects a box over the antimeridian."""
    dlat = km / 111.32
    dlon = km / (111.32 * max(abs(math.cos(math.radians(near.lat))), 0.2))
    return (
        f"{near.lon - dlon:.5f},{near.lat - dlat:.5f},"
        f"{near.lon + dlon:.5f},{near.lat + dlat:.5f}"
    )


def _nearby_airport(query: str) -> bool:
    words = re.findall(r"[a-z]+", (query or "").casefold())
    core = [word for word in words if word not in _AIRPORT_FILL]
    return len(core) == 1 and core[0] in _AIRPORT_WORDS


def clean_place_correction(text: str | None) -> str | None:
    """First line of a hearing correction, or None when it is not a place name."""
    if not text:
        return None
    line = str(text).strip().splitlines()[0].strip().strip(" \"'`“”")
    line = re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", line)          # "1. Name", "- Name"
    line = re.sub(r"[.!?]+$", "", line).strip()
    if not line or len(line) > 80 or len(line.split()) > 8:
        return None
    folded = line.casefold()
    if any(bad in folded for bad in _CORRECTION_REJECT):
        return None
    return line


# "CVS pharmacy near Palo Alto", "the pharmacy opposite us": where it is, not what it is called.
_RELATIVE_TAIL = re.compile(r"\s+(?:near(?:by)?|opposite(?:\s+to)?|next\s+to|close\s+to|across\s+from|"
                            r"beside|around)\b.*$", re.I)
# Street words: "CVS Pharmacy Middlefield Road Palo Alto" is CVS Pharmacy, on Middlefield Road.
_STREET_WORDS = frozenset({"road", "rd", "street", "st", "avenue", "ave", "boulevard", "blvd", "way", "drive",
                           "dr", "lane", "ln", "expressway", "expy", "highway", "hwy", "parkway", "pkwy"})


def _before_street(query: str) -> str:
    """The name before a trailing street ("... Middlefield Road ..."), or "" when there is none."""
    words = query.split()
    for i, word in enumerate(words):
        if i >= 2 and _alnum(word) in _STREET_WORDS:
            return " ".join(words[:i - 1])
    return ""


def _without_locality(query: str) -> str:
    """Drop a trailing ", City" or "in City" so the place name can still match.

    "Paris, Texas" stays whole: the part before the comma is a single word, and that
    word is the city. "Bowling Green Park, Palo Alto" becomes "Bowling Green Park".
    """
    text = _RELATIVE_TAIL.sub("", re.sub(r"\s+", " ", (query or "").strip())) or (query or "").strip()
    for _ in range(3):
        match = re.match(r"^(?P<name>.+?)\s*,\s*(?P<where>[^,]+)$", text)
        if match is None:
            match = re.match(r"^(?P<name>.+?)\s+\bin\s+(?P<where>.+)$", text, re.I)
        if match is None:
            break
        name, where = match.group("name").strip(), match.group("where").strip()
        if not (len(name.split()) >= 2 and 1 <= len(where.split()) <= 4):
            break
        text = name
    return text


def _name_part(query: str) -> str:
    """The name to score results by: "Dar, Los Gatos" is the place "Dar" in Los Gatos.

    Unlike the search form, a one-word name is split from its town here. Searching
    keeps "Paris, Texas" whole; scoring the results by "Paris" finds the same city.
    """
    text = _RELATIVE_TAIL.sub("", re.sub(r"\s+", " ", (query or "").strip())) or (query or "").strip()
    match = re.match(r"^(?P<name>[^,]+?)\s*,\s*(?P<where>[^,]+)$", text)
    if match is None:
        return _without_locality(text)
    name, where = match.group("name").strip(), match.group("where").strip()
    return name if name and 1 <= len(where.split()) <= 4 else text


def _query_forms(query: str) -> list[str]:
    """Names to search, the one we score first.

    The scored form has no trailing city. A shorter form drops one trailing
    place-kind word ("Bowling Green Park" → "Bowling Green") and is searched
    only inside the tight box, so a far street of the longer name cannot win
    just by being the first hit over the floor.
    """
    # A spoken article ("the OpenAI office") steers Search Box to names starting with "The".
    primary = re.sub(r"^\s*(the|a|an)\s+", "", _without_locality(query), flags=re.I)
    forms = [primary] if primary else []
    named = _before_street(primary)
    if named:   # searched as itself too: the full line finds the street, not the shop on it
        forms.append(named)
    words = primary.split()
    if len(words) >= 3 and _alnum(words[-1]) in _TAIL_WORDS:
        stem = " ".join(words[:-1]).strip()
        if stem and stem.casefold() != primary.casefold():
            forms.append(stem)
    return forms or [query.strip()]


def _is_address(text: str) -> bool:
    return re.match(r"^\d{1,6}\s+[A-Za-z]", (text or "").strip()) is not None


def _usable_correction(heard: str, rewritten: str) -> bool:
    """A hearing fix may rename the place. It may not expand it into a different venue."""
    if _is_address(rewritten):
        return True
    if name_score(heard, rewritten) >= _GLOBAL_SCORE:
        return True
    return len(rewritten.split()) <= len((heard or "").split()) + 1


def _address_span(text: str) -> str | None:
    """The street address inside a line that also repeats the place name."""
    match = re.search(r"\b\d{1,6}\s+[A-Za-z]", text or "")
    if match is None:
        return None
    span = (text or "")[match.start():].strip()
    return span or None


def clean_place_corrections(text: str | None, limit: int = 3) -> list[str]:
    """Up to ``limit`` place names, one per line, most likely first."""
    names: list[str] = []
    for raw in str(text or "").splitlines():
        name = clean_place_correction(raw)
        if name is None:
            # "Palo Alto Art Center, 1313 Newell Rd, Palo Alto, CA" is one long line.
            span = _address_span(raw)
            if span:
                name = clean_place_correction(span)
        if name and name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def hear_place_prompt(
    area: str, lat: float, lon: float, heard: str, utterance: str = "", destination: str = "",
) -> str:
    """Ask for a place name or its street address. The car's area is the context.

    No destination list is supplied. The model has to use the area and the utterance.
    A civic building can be missing from the map under the name the driver said.
    """
    name = (area or "").strip() or "the current area"
    said = (utterance or "").strip()
    context = ""
    if said and _alnum(said) != _alnum(heard):
        context = f"The driver said: {said}\n"
    heading = (destination or "").strip()
    route = f"The car is driving to {heading}. " if heading else ""
    return (
        f"The car is at {name} ({float(lat):.4f}, {float(lon):.4f}). {route}"
        f"{context}"
        f"Speech recognition heard this place: {(heard or '').strip()}\n"
        "A map search found no place spelled like that near the car. "
        "The words may be a mishearing of a nearby place whose name sounds alike, "
        "or the place may be real and the map may only list its street address.\n"
        "Reply with up to three lines, one per line, most likely first, and nothing else. "
        "If you know the street address of that exact place near the car, put it first: "
        "house number, street, and city. "
        "Do not substitute a different venue that only shares some of the words. "
        "Otherwise name the nearby places a driver there most likely wants. "
        "If they want the nearest airport, reply: airport"
    )


def cloud_location_sentence(area: str, lat: float, lon: float, *, gps_fix: bool) -> str:
    """One line for the cloud prompt: where the car is, and that place names may be misheard."""
    name = (area or "").strip() or "the current area"
    where = f"near {name} ({float(lat):.4f}, {float(lon):.4f})"
    lead = (f"The car is {where}. " if gps_fix else
            f"There is no GPS fix: the car's position is simulated, {where}; say so if asked where it is. ")
    return (
        lead +
        "Speech often mishears place names. When the driver asks for a place or directions, "
        "call vehicle_navigation with the place a driver there most likely means. "
        "Do not ask them to spell it before searching. "
    )


def _feature_place(feature: dict[str, Any], category: str | None) -> Place | None:
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") or {}
    name = str(props.get("name") or props.get("name_preferred") or "").strip()
    lon = lat = None
    geom = (feature.get("geometry") or {}).get("coordinates") or ()
    if len(geom) >= 2:
        lon, lat = geom[0], geom[1]
    else:
        coords = props.get("coordinates") or {}
        lat, lon = coords.get("latitude"), coords.get("longitude")
    if not name or lat is None or lon is None:
        return None
    return Place(name, float(lat), float(lon), category)


def _places_from(payload: Any, category: str | None) -> list[Place]:
    if not isinstance(payload, dict):
        return []
    places: list[Place] = []
    for feature in payload.get("features") or []:
        place = _feature_place(feature, category)
        if place is not None:
            places.append(place)
    return places


# Names this close to the best score are the same place to a driver: take the nearest.
# A town in the query ("... New Mission") lifts its own name above the margin.
_TIE_MARGIN = 0.12


def _key_word(query: str) -> str:
    """The word that names the place: the first that is neither an article nor a kind word."""
    for word in re.findall(r"[a-z0-9]+", (query or "").casefold()):
        if word not in _ARTICLES and word not in _KIND_WORDS:
            return word
    return ""


_KIND_PHRASES = ("post office", "gas station", "car wash", "coffee shop", "police station", "fire station",
                 "train station", "bus station", "grocery store", "urgent care")


def _kind_clash(query: str, candidate: str) -> bool:
    """The query names a kind of venue and the candidate is another kind, not that one."""
    q, cand = (query or "").casefold(), (candidate or "").casefold()
    for phrase in _KIND_PHRASES:   # "Palo Alto post office" is not the "Palo Alto Daily Post"
        if q.rstrip(" .").endswith(phrase):
            return phrase not in cand
    said = re.findall(r"[a-z0-9]+", (query or "").casefold())
    if not said or said[-1] not in _VENUE_KINDS:
        return False
    kinds = {w for w in re.findall(r"[a-z0-9]+", (candidate or "").casefold()) if w in _VENUE_KINDS}
    return bool(kinds) and said[-1] not in kinds and said[-1].rstrip("s") not in {k.rstrip("s") for k in kinds}


def _has_key_word(key: str, candidate: str) -> bool:
    if not key:
        return True
    return any(difflib.SequenceMatcher(None, key, word).ratio() >= _KEY_WORD_MATCH
               for word in re.findall(r"[a-z0-9]+", (candidate or "").casefold()))


def _place_score(query: str, candidate: str) -> float:
    """name_score, also without trailing kind words: "OpenAI office" is a full match for "OpenAI"."""
    score = name_score(query, candidate)
    words = query.split()
    while words and _alnum(words[-1]) in _KIND_WORDS:
        words.pop()
    core = " ".join(words)
    if core and core != query.strip():
        score = max(score, name_score(core, candidate))
    named = _before_street(query)
    if named:
        score = max(score, name_score(named, candidate))
    return score


def _best_place(query: str, places: list[Place], near: Place, floor: float) -> Place | None:
    scored: list[tuple[float, float, Place]] = []
    key = _key_word(query)
    for place in places:
        score = _place_score(query, place.name)
        if score < _EXACT_SCORE and (not _has_key_word(key, place.name) or _kind_clash(query, place.name)):
            continue   # close in letters, but not the place named
        if score >= floor:
            scored.append((score, haversine_km(near, place), place))
    if not scored:
        return None
    best = max(row[0] for row in scored)
    ties = [row for row in scored if row[0] >= best - _TIE_MARGIN]
    # The place with exactly the name said beats a nearer one that only starts
    # with it: "Golden Gate Bridge", not "Golden Gate Bridge Bike Rentals".
    said = _alnum(re.sub(r"^\s*(the|a|an)\s+", "", query or "", flags=re.I))
    ties.sort(key=lambda row: (_alnum(row[2].name) != said, row[1], -row[0]))
    return ties[0][2]


class MapboxNav:
    """Live place search and driving-traffic ETAs. NavFeed draws the road.

    ``route`` asks Directions once for the whole trip. A failed call keeps the
    straight-line estimate. The message and the URL are never logged.

    Search Box ``/forward`` is biased to the car, then filtered by name. A wide box
    reaches nearby towns. An almost-exact wide hit is accepted immediately. A weaker
    wide hit is pooled with the tight box and with one shorter local name ("Bowling
    Green" for "Bowling Green Park"), and the nearer place wins a near tie. A global
    query is accepted only when the name is almost exact, so a far city still works.
    One optional ``corrector`` runs after that miss. A street address it offers is
    kept only inside the tight box, under the name the driver said.

    ``fetch`` is a test seam: ``(path, params) -> JSON``. Failures log the exception
    type only — HTTP errors carry the token in the URL.
    """

    is_live = True

    def __init__(
        self, token: str, *, timeout_s: float = 4.0, limit: int = 3,
        fetch: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        corrector: Callable[[str, Place], str | None] | None = None,
    ):
        self._token = token
        self._timeout = float(timeout_s)
        self._limit = int(limit)
        self._fetch_fn = fetch
        self.corrector = corrector
        self._fallback = FixtureNav()

    def search(
        self, query: str, near: Place, category: str | None = None, along_route: bool = False,
        *, route: tuple[LatLon, ...] = (), limit: int | None = None,
    ) -> list[Place]:
        del along_route  # the route itself says where to look
        if category:
            return self._by_category(category, near, route=route, limit=limit)
        heard = (query or "").strip()
        if not heard:
            return []
        if _nearby_airport(heard):
            return self._by_category("airport", near)
        found = self._ranked(heard, near)
        if found or self.corrector is None:
            return found
        # Each proposed name has to resolve near the car; a wrong guess just misses.
        # An address keeps the driver's name: the map often has the building only as a street.
        label = _query_forms(heard)[0]
        for rewritten in self._rewrite(heard, near):
            if not _usable_correction(heard, rewritten):
                continue
            if _nearby_airport(rewritten):
                return self._by_category("airport", near)
            if _is_address(rewritten):
                found = self._local_exact(rewritten, near)
                if found:
                    hit = found[0]
                    return [Place(label, hit.lat, hit.lon, hit.category)]
                continue
            found = self._ranked(rewritten, near)
            if found:
                return found
        return []

    def route(self, origin: Place, stops: list[Place]) -> Route:
        if stops:
            from nova_hailo.cabingym.directions import mapbox_planner

            planned = mapbox_planner(self._token)(
                [(origin.lat, origin.lon), *[(s.lat, s.lon) for s in stops]]
            )
            if planned is not None and planned.distance_m > 0:
                return Route(
                    round(planned.distance_m / 1000.0, 1),
                    round(planned.duration_s / 60.0, 1),
                    tuple(stops),
                )
        return self._fallback.route(origin, stops)

    def _ranked(self, query: str, near: Place) -> list[Place]:
        forms = _query_forms(query)
        primary = forms[0]
        name = _name_part(query)
        # An exact wide hit is the place (a nearby town, or the name itself).
        wide = self._forward(primary, near, _bbox(near, _WIDE_KM))
        exact = _best_place(name, wide, near, _EXACT_SCORE)
        if exact is not None:
            return [exact]
        tight = self._forward(primary, near, _bbox(near, _TIGHT_KM))
        # The shorter name stays inside the tight box. A far street must not outrank a local POI.
        extra = [p for form in forms[1:] for p in self._forward(form, near, _bbox(near, _TIGHT_KM))]
        hit = _best_place(name, [*wide, *tight, *extra], near, _LOCAL_SCORE)
        if hit is not None:
            return [hit]
        far = self._forward(primary, near, None)
        hit = _best_place(name, far, near, _GLOBAL_SCORE)
        return [hit] if hit is not None else []

    def _local_exact(self, query: str, near: Place) -> list[Place]:
        """A corrected street address, only when that address is near the car."""
        primary = _query_forms(query)[0]
        places = self._forward(primary, near, _bbox(near, _TIGHT_KM))
        hit = _best_place(primary, places, near, _GLOBAL_SCORE)
        if hit is None or haversine_km(near, hit) > _TIGHT_KM:
            return []
        return [hit]

    def _rewrite(self, query: str, near: Place) -> list[str]:
        try:
            raw = self.corrector(query, near) if self.corrector is not None else None
        except Exception as exc:  # noqa: BLE001 -- the message can contain the token
            logger.info("mapbox_nav:correct_failed %s", type(exc).__name__)
            return []
        return [name for name in clean_place_corrections(raw) if _alnum(name) != _alnum(query)]

    def _by_category(
        self, category: str, near: Place, *, route: tuple[LatLon, ...] = (), limit: int | None = None,
    ) -> list[Place]:
        canonical = _CATEGORY_IDS.get(category)
        if canonical:
            params: dict[str, Any] = {
                "proximity": f"{near.lon:.6f},{near.lat:.6f}",
                "limit": 25,
                "language": "en",
                "access_token": self._token,
            }
            params.update(self._route_params(route))
            payload = self._call(f"/category/{canonical}", params)
            if not _places_from(payload, category):
                # An id the index has nothing for must not look like "no such place nearby".
                text = _CATEGORY_TEXT.get(category, category.replace("_", " "))
                params = self._forward_params(text, near, None if route else _bbox(near, _WIDE_KM), poi=True)
                params.update(self._route_params(route))
                payload = self._call("/forward", params)
        else:
            params = self._forward_params(category, near, None if route else _bbox(near, _WIDE_KM), poi=True)
            params.update(self._route_params(route))
            payload = self._call("/forward", params)
        places = _places_from(payload, category)
        places.sort(key=lambda place: haversine_km(near, place))
        return places[: limit or self._limit]

    @staticmethod
    def _route_params(route: tuple[LatLon, ...]) -> dict[str, Any]:
        """Search Box along a route: places within ROUTE_DETOUR_MIN of driving off it."""
        if len(route) < 2:
            return {}
        from nova_hailo.cabingym.drive import encode_polyline

        return {
            "route": encode_polyline(list(route), 6),
            "route_geometry": "polyline6",
            "sar_type": "isochrone",
            "time_deviation": ROUTE_DETOUR_MIN,
        }

    def drive_minutes(self, origin: Place, places: list[Place]) -> list[float | None]:
        """Driving minutes with traffic from ``origin`` to each place (Matrix API, one call).
        None for a place the matrix could not reach; straight-line estimates if the call fails."""
        if not places:
            return []
        places = places[:_MATRIX_MAX - 1]
        coords = ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in [origin, *places])
        payload = self._call(f"{_MATRIX_PATH}/{coords}",
                             {"sources": "0", "annotations": "duration", "access_token": self._token})
        rows = payload.get("durations") if isinstance(payload, dict) else None
        if not rows or not isinstance(rows[0], list) or len(rows[0]) != len(places) + 1:
            return self._fallback.drive_minutes(origin, places)
        return [None if d is None else round(float(d) / 60.0, 1) for d in rows[0][1:]]

    def _forward(self, query: str, near: Place, bbox: str | None) -> list[Place]:
        return _places_from(self._call("/forward", self._forward_params(query, near, bbox, poi=False)), None)

    def _forward_params(self, query: str, near: Place, bbox: str | None, *, poi: bool) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query[:256],
            "proximity": f"{near.lon:.6f},{near.lat:.6f}",
            "limit": 10,
            "types": "poi" if poi else _FORWARD_TYPES,
            "auto_complete": "true",
            "language": "en",
            "access_token": self._token,
        }
        if bbox:
            params["bbox"] = bbox
        return params

    def _call(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = self._fetch(path, params)
        except Exception as exc:  # noqa: BLE001 -- the message can contain the token
            logger.info("mapbox_nav:search_failed %s", type(exc).__name__)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _fetch(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._fetch_fn is not None:
            return self._fetch_fn(path, params)
        import httpx

        base = _API_BASE if path.startswith(_MATRIX_PATH) else _SEARCH_BASE
        response = httpx.get(base + path, params=params, timeout=self._timeout)
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {}


def mapbox_nav_from_env() -> MapboxNav | None:
    """A live search provider when a Mapbox token is set, otherwise None."""
    token = mapbox_token()
    return MapboxNav(token) if token else None
