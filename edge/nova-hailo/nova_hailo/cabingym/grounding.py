# nova_hailo/cabingym/grounding.py
"""Turn text (or cloud tool args) into grounded API arguments.

Every argument records where it came from: enum (router choice), entity
(something in vehicle state), parser, span, default, or cloud. Anything
that cannot be grounded is reported as missing (→ clarify) or unsupported
(→ decline); nothing is guessed. Callers: cabingym.router_regex,
.router_jev, .cloud_tools.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym import slots as s
from nova_hailo.cabingym.devices.base import Api
from nova_hailo.cabingym.nav import Place
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall

if TYPE_CHECKING:
    from nova_hailo.cabingym.runtime import CabinEnv

NONE_OF_THESE = "none_of_these"
Choose = Callable[[str], "str | None"]


def _count(text: str) -> dict[str, int] | None:
    n = s.parse_number(text)
    return {"value": int(n)} if n is not None and 1 <= n <= 5 else None


NUMERIC_PARSERS: dict[str, Callable[[str], Mapping[str, Any] | None]] = {
    "temperature": s.parse_temperature,
    "percent": s.parse_percent,
    "opening": s.parse_opening,
    "shade": s.parse_shade,
    "fan_level": lambda t: s.parse_level(t, 6, 3),
    "seat_level": lambda t: s.parse_level(t, 3, 2),
    "count": _count,
    "within_km": s.parse_within_km,
    "within_min": s.parse_within_min,
}
BOOL_PARSERS: dict[str, Callable[[str], bool | None]] = {
    "on_off": s.parse_on_off, "lock": s.parse_lock, "fold": s.parse_fold,
    "recirc": s.parse_recirc, "mute": s.parse_mute, "along_route": s.parse_along_route,
    "after": s.parse_after,
}
SPAN_PARSERS: dict[str, Callable[[str], str | None]] = {
    "message": s.extract_message, "media": s.extract_media_query,
}
_REARISH = frozenset({"rear", "rear_left", "rear_right", "back"})


@dataclass
class Grounded:
    args: dict[str, Any] = field(default_factory=dict)
    grounding: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    # A name the driver did say, which search could not resolve. Distinct from an empty arg.
    unresolved: dict[str, str] = field(default_factory=dict)

    def ok(self) -> bool:
        return not self.missing and not self.unsupported


def _set(g: Grounded, name: str, value: Any, how: str) -> None:
    g.args[name] = value
    g.grounding[name] = how


def entity_names(kind: str, snapshot: Mapping[str, Any]) -> list[str]:
    if kind == "contacts":
        return [str(x["name"]) for x in snapshot.get(c.CONTACTS, ())]
    if kind == "senders":
        out: list[str] = []
        for m in snapshot.get(c.MESSAGES, ()):
            if m["from"] not in out:
                out.append(str(m["from"]))
        return out
    return []


def here(snapshot: Mapping[str, Any]) -> Place:
    return Place("here", float(snapshot[c.LAT]), float(snapshot[c.LON]))


def _place(d: Mapping[str, Any]) -> dict[str, Any]:
    return Place.from_dict(dict(d)).to_dict()


def place_candidates(snapshot: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(spoken name, place) in priority order: saved places, contacts' places, last results."""
    out = [(str(p["name"]), _place(p)) for p in snapshot.get(c.NAV_SAVED, ())]
    for contact in snapshot.get(c.CONTACTS, ()):
        if contact.get("place"):
            out.append((str(contact["name"]), _place(contact["place"])))
    out += [(str(p["name"]), _place(p)) for p in snapshot.get(c.NAV_RESULTS, ())]
    return out


_PLACE_SPAN = re.compile(
    r"\b(?:navigate\s+to|directions\s+to|take\s+me\s+to|drive\s+(?:me\s+)?to|head\s+to|go\s+to|"
    r"route\s+(?:me\s+)?to|get\s+me\s+to|add\s+(?:a\s+)?(?:new\s+)?stop\s+at|"
    r"stop\s+(?:off\s+)?at|swing\s+by|pull\s+by|"
    r"pick\s+up\s+(?:\w+\s+){1,3}?at|drop\s+(?:\w+\s+){0,3}?off\s+at)"
    r"\s+(?:the\s+)?(?P<q>.+?)\s*[.!?]*$", re.I)
# What follows the place in a spoken stop request, not part of its name.
_PLACE_TAIL = re.compile(
    r"\s+(?:(?:on|along)\s+(?:the|our|my)\s+way|as\s+a\s+(?:new\s+)?stop|at\s+a\s+stop|"
    r"to\s+(?:pick|drop)\b|right\s+now|please|via|by\s+way\s+of|passing\s+through|going\s+through)\b.*$",
    re.I)


# "the second one" names a place from the last search (Nav.LastResults).
_ORDINALS = {"first": 0, "1st": 0, "closest": 0, "nearest": 0, "second": 1, "2nd": 1,
             "third": 2, "3rd": 2, "last": -1}
_ORDINAL = re.compile(r"\b(first|1st|closest|nearest|second|2nd|third|3rd|last)\s+(one|option|place|result)\b",
                      re.I)


# "Barbara Drive in Los Gatos" is searched as "Barbara Drive, Los Gatos", the form the place
# search reads as a name plus the town to look in.
_IN_TOWN = re.compile(r"^(?P<name>.+?)\s+in\s+(?P<town>[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,2})\s*$")
_VIA = re.compile(r"\b(?:via|by\s+way\s+of|passing\s+through|going\s+through)\s+(?P<v>.+?)\s*[.!?]*$", re.I)


def town_form(name: str) -> str:
    m = _IN_TOWN.match((name or "").strip())
    return f"{m.group('name')}, {m.group('town')}" if m else (name or "").strip()


_HIGHWAY = re.compile(r"\b(?:us|i|ca|sr|hwy|highway|interstate|route|freeway)?[-\s]?\d{1,3}\b|"
                      r"\b(?:highway|freeway|interstate)\b", re.I)


def split_places(value: Any) -> list[str]:
    """['Los Altos', 'Cupertino'] or "Los Altos, Cupertino and Campbell" -> names in order."""
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        for part in re.split(r"\s*,\s*|\s+(?:and|then)\s+", str(item or "")):
            part = part.strip(" .!?")
            if part:
                out.append(part)
    return out


def parse_via(text: str) -> list[str]:
    m = _VIA.search(text or "")
    return split_places(m.group("v")) if m else []


def resolve_place(text: str, cabin: CabinEnv) -> tuple[dict[str, Any] | None, str]:
    snap = cabin.store.snapshot()
    cands = place_candidates(snap)
    hit = s.find_entity_in_text(text, [name for name, _ in cands])
    if hit is not None:
        return next(p for n, p in cands if n == hit), "entity"
    results = snap.get(c.NAV_RESULTS) or ()
    pick = _ORDINAL.search(text) if results else None
    if pick is not None:
        index = _ORDINALS[pick.group(1).lower()]
        if index < len(results) or index == -1:
            return _place(results[index]), "entity"
    m = _PLACE_SPAN.search(text)
    if m and cabin.allow_poi_search:
        query = town_form(_PLACE_TAIL.sub("", m.group("q")).strip(" .!?") or m.group("q"))
        found = cabin.nav.search(query, here(snap))
        if found:
            return found[0].to_dict(), "span"
    return None, "missing"


# A name close to one place in the last search results wins over a fresh search.
_HEARD_MIN = 0.6
_HEARD_MARGIN = 0.2


def _sounds_like_a_result(name: str, snap: Mapping[str, Any]) -> dict[str, Any] | None:
    from nova_hailo.cabingym.nav import name_score

    results = [r for r in (snap.get(c.NAV_RESULTS) or ()) if isinstance(r, Mapping) and r.get("name")]
    if not results:
        return None
    scored = sorted(((name_score(name, str(r["name"])), i) for i, r in enumerate(results)), reverse=True)
    best, i = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if best >= _HEARD_MIN and best - runner >= _HEARD_MARGIN:
        return _place(results[i])
    return None


def _place_by_name(name: str, cabin: CabinEnv) -> dict[str, Any] | None:
    snap = cabin.store.snapshot()
    key = name.strip().casefold()
    cands = place_candidates(snap)
    for spoken, place in cands:
        if key in (spoken.casefold(), str(place["name"]).casefold()):
            return place
    hit = s.match_entity(name, [n for n, _ in cands])
    if hit is not None:
        return next(p for n, p in cands if n == hit)
    heard = _sounds_like_a_result(name, snap)
    if heard is not None:
        return heard
    if cabin.allow_poi_search:
        found = cabin.nav.search(town_form(name), here(snap))
        if found:
            return found[0].to_dict()
    return None


def _ground_places(g: Grounded, pname: str, names: list[str], cabin: CabinEnv, how: str = "parser") -> None:
    """Each name resolved like a destination. One that cannot be found makes the call ask:
    a route that silently skips a "via" is the wrong route."""
    if not names:
        return
    places, skipped = [], []
    for name in dict.fromkeys(names):
        place = _place_by_name(name, cabin)
        if place is None and _HIGHWAY.search(name):
            skipped.append(name)      # a road by number cannot be searched as a place
            continue
        if place is None:
            g.missing.append(pname)
            g.unresolved[pname] = name
            return
        places.append(place)
    _set(g, pname, places, how)
    if skipped:
        g.args["_skipped_via"] = skipped


def _finish(api: Api, g: Grounded) -> Grounded:
    for name in api.required:
        if name not in g.args and name not in g.missing and name not in g.unsupported:
            g.missing.append(name)
    if api.requires_any and not any(k in g.args for k in api.requires_any):
        g.missing.append("|".join(api.requires_any))
    return g


# Topics that tell what a sentence is about. "drive" (speed, range) and "cabin" (inside,
# people) are questions about the car, not a request to change another control.
_TOPICS = ("hvac", "lights", "media", "seats", "windows", "doors", "body", "system", "phone", "navigation")


def _about(cabin: CabinEnv, text: str) -> set[str]:
    return {name for name in _TOPICS
            if name in cabin.devices and re.search(cabin.devices[name].keywords, text or "", re.I)}


def asked_for(cabin: CabinEnv, device: str, api: Api, text: str) -> bool:
    """False only when the sentence, or failing that the last turn, is about another control."""
    if not (text or "").strip():
        return True
    if device in _about(cabin, text) or any(re.search(p, text, re.I) for p in api.patterns):
        return True
    if _about(cabin, text) - {device}:
        return False
    for turn in reversed(cabin.dialog or []):
        prev = str(turn.get("user") or "")
        if prev.strip():
            about = _about(cabin, prev)
            return not about or device in about
    return True


def _split_via(api: Api, raw: dict[str, Any]) -> dict[str, Any]:
    """A model that writes "Lucie Stern Community Center via Hopkins Avenue" as the destination
    means a destination and a place to pass through."""
    dest = raw.get("destination")
    if "via" not in api.params or not isinstance(dest, str):
        return raw
    m = _VIA.search(dest)
    if not m or not dest[:m.start()].strip(" ,"):
        return raw
    raw["destination"] = dest[:m.start()].strip(" ,")
    names = split_places(m.group("v")) + split_places(raw.get("via") or [])
    raw["via"] = list(dict.fromkeys(n.strip() for n in names if n.strip()))
    return raw


_STOP_AT = re.compile(r"\bstop\s+(?:off\s+)?(?:at|by)\s+\S", re.I)
_FIRST_THEN = re.compile(
    r"\bfirst\b[,\s]*(?:let'?s\s+)?(?:go\s+to\s+|stop\s+at\s+|head\s+to\s+|to\s+)?(?:the\s+)?(?P<a>.+?)"
    r"\s*,?\s+(?:and\s+)?then\s+(?:go\s+to\s+|head\s+to\s+|to\s+)?(?:the\s+)?(?P<b>.+?)\s*[.!?]*$", re.I)


def _names_current_destination(name: str, snap: Mapping[str, Any]) -> bool:
    """ "the theatre" while navigating to Landmark's Aquarius Theatre."""
    dest = str(snap.get(c.NAV_DEST) or "")
    words = set(re.findall(r"[a-z]+", name.lower())) - {"the", "a", "an", "back", "there"}
    return bool(dest and snap.get(c.NAV_ACTIVE) and words and words <= set(re.findall(r"[a-z]+", dest.lower())))


def _named(query: str) -> bool:
    """ "the Bank of America" names one place; "a bank" names a kind."""
    words = re.findall(r"[A-Za-z']+", query)
    return any(w[0].isupper() for w in words if w.lower() not in {"the", "a", "an", "i"})


def correct_nav_call(cabin: CabinEnv, text: str, outcome: RouteOutcome) -> RouteOutcome:
    """Fix a navigation call the sentence contradicts: "stop at X", "first X, then Y", "go to <name>"."""
    if outcome.kind != "vehicle" or len(outcome.calls) != 1 or not (text or "").strip():
        return outcome
    call = outcome.calls[0]
    if call.device != "navigation":
        return outcome
    snap = cabin.store.snapshot()
    fixed: VehicleCall | None = None
    if call.api == "stop_navigation" and _STOP_AT.search(text):
        place, how = resolve_place(text, cabin)
        if place is not None:
            api = "add_stop" if snap.get(c.NAV_ACTIVE) else "navigate_to"
            fixed = VehicleCall("navigation", api, {"destination": place}, {"destination": how}, call.score)
    elif call.api == "find_nearby" and (span := _PLACE_SPAN.search(text)) and _named(span.group("q")):
        from nova_hailo.cabingym.nav import name_score

        place, how = resolve_place(text, cabin)
        query = _PLACE_TAIL.sub("", span.group("q")).strip(" .!?")
        if place is not None and name_score(query, str(place.get("name") or "")) >= 0.6:
            fixed = VehicleCall("navigation", "navigate_to", {"destination": place}, {"destination": how}, call.score)
    elif call.api == "navigate_to" and isinstance(call.args.get("destination"), Mapping):
        m = _FIRST_THEN.search(text)
        if m:
            from nova_hailo.cabingym.nav import name_score

            dest = call.args["destination"]
            first, then = m.group("a").strip(" ,"), m.group("b").strip(" ,")
            if name_score(first, str(dest.get("name") or "")) >= 0.5:
                if _names_current_destination(then, snap):
                    final = {"name": snap[c.NAV_DEST], "lat": snap[c.NAV_DEST_LAT], "lon": snap[c.NAV_DEST_LON]}
                else:
                    final = _place_by_name(then, cabin)
                if final is not None and str(final.get("name")) != str(dest.get("name")):
                    args = dict(call.args)
                    args["destination"] = final
                    args["via"] = [dest, *list(call.args.get("via") or ())]
                    fixed = VehicleCall("navigation", "navigate_to", args,
                                        {**call.grounding, "destination": "first_then"}, call.score)
    if fixed is None:
        return outcome
    return RouteOutcome("vehicle", [fixed], outcome.speak, {**outcome.trace, "corrected": call.api})


def mark_unasked(cabin: CabinEnv, text: str, outcome: RouteOutcome) -> RouteOutcome:
    """Tag cloud writes to a cabin control the sentence did not ask for; the gate (G10) then
    confirms them instead of acting. Reads and navigation are left alone."""
    if outcome.kind != "vehicle" or not outcome.calls or not (text or "").strip():
        return outcome
    calls: list[VehicleCall] = []
    changed = False
    for call in outcome.calls:
        api = cabin.devices[call.device].apis[call.api]
        if call.device in _TOPICS and call.device not in ("navigation",) and not api.read_only \
                and not asked_for(cabin, call.device, api, text):
            calls.append(VehicleCall(call.device, call.api, dict(call.args),
                                     {**call.grounding, "asked": "no"}, call.score))
            changed = True
        else:
            calls.append(call)
    return RouteOutcome("vehicle", calls, outcome.speak, dict(outcome.trace)) if changed else outcome


def _device_of(cabin: CabinEnv, api: Api) -> str | None:
    for name, d in cabin.devices.items():
        if d.apis.get(api.name) is api:
            return name
    return None


def ground_args(api: Api, text: str, cabin: CabinEnv, choose: Choose | None = None) -> Grounded:
    snap = cabin.store.snapshot()
    g = Grounded()
    parsed: set[str] = set()
    for pname, spec in api.params.items():
        kind = str(spec.get("x-ground", ""))
        picked = choose(pname) if choose is not None else None
        if kind == "zone":
            named, how = s.parse_zone(text, spec["enum"], default=None)
            if how == "unsupported":
                g.unsupported.append(pname)
                continue
            default = spec.get("default")
            focus = cabin.focused_zone(api.name, spec["enum"])
            dev = _device_of(cabin, api)
            said, about = cabin.dialog_zone(spec["enum"])
            # A side the driver gave for another control ("the seat, driver side only") is not
            # this one's: a model that carries it over would change only half the cabin.
            elsewhere = bool(about) and dev not in about
            past = said if s.continues_request(text) and not elsewhere else None
            carried = named is None and picked == said and elsewhere
            if named is not None and picked == named:
                _set(g, pname, picked, "enum")
            elif named is not None:
                _set(g, pname, named, "parser")
            elif focus and s.continues_request(text):
                _set(g, pname, focus, "focus")
            elif past:
                _set(g, pname, past, "dialog")
            # A side the router model guessed with none said, in focus or in the dialogue
            # ("set the fan speed to one" -> driver) changed half the cabin: the default wins.
            elif default in spec["enum"]:
                _set(g, pname, default, "default")
            elif picked in spec["enum"] and not carried:
                _set(g, pname, picked, "enum")
        elif kind == "enum":
            if picked in spec["enum"]:
                _set(g, pname, picked, "enum")
                continue
            val = s.parse_enum(text, spec["enum"], spec.get("x-synonyms"), spec.get("x-bool-map"))
            if val is not None:
                _set(g, pname, val, "parser")
            elif "default" in spec:
                _set(g, pname, spec["default"], "default")
        elif kind.startswith("bool:"):
            flag = BOOL_PARSERS[kind[5:]](text)
            if flag is not None:
                _set(g, pname, flag, "parser")
            elif "default" in spec:
                _set(g, pname, spec["default"], "default")
        elif kind in NUMERIC_PARSERS:
            if kind in parsed:
                continue
            parsed.add(kind)
            out = NUMERIC_PARSERS[kind](text)
            if not out:
                continue
            group = [n for n, sp in api.params.items() if sp.get("x-ground") == kind]
            for key, val in out.items():
                target = key if key in group else (group[0] if len(group) == 1 else None)
                if target is not None:
                    _set(g, target, val, "parser")
        elif kind.startswith("entity:"):
            if picked == NONE_OF_THESE:
                g.missing.append(pname)
                continue
            names = entity_names(kind[7:], snap)
            if picked in names:
                _set(g, pname, picked, "entity")
                continue
            hit = s.find_entity_in_text(text, names)
            if hit is not None:
                _set(g, pname, hit, "entity")
        elif kind.startswith("span:"):
            val = SPAN_PARSERS[kind[5:]](text)
            if val:
                _set(g, pname, val, "span")
        elif kind == "places":
            _ground_places(g, pname, parse_via(text), cabin)
        elif kind == "place":
            if picked == NONE_OF_THESE:
                g.missing.append(pname)
                continue
            by_name = dict(place_candidates(snap))
            if picked in by_name:
                _set(g, pname, by_name[picked], "entity")
                continue
            place, how = resolve_place(text, cabin)
            if place is not None:
                _set(g, pname, place, how)
            else:
                span = _PLACE_SPAN.search(text)
                if span:
                    query = _PLACE_TAIL.sub("", span.group("q")).strip(" .!?")
                    if query:
                        g.unresolved.setdefault(pname, query)
    return _finish(api, g)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "on", "yes", "1"):
        return True
    if text in ("false", "off", "no", "0"):
        return False
    return None


def normalize_cloud_args(api: Api, raw: Mapping[str, Any] | None, cabin: CabinEnv) -> Grounded:
    snap = cabin.store.snapshot()
    g = Grounded()
    raw = _split_via(api, dict(raw or {}))
    for pname, value in raw.items():
        spec = api.params.get(pname)
        if spec is None or value is None or value == "":
            continue
        kind = str(spec.get("x-ground", ""))
        if "enum" in spec:
            v = str(value).strip().lower()
            if v in spec["enum"]:
                _set(g, pname, v, "cloud")
            elif kind == "zone" and v in _REARISH:
                g.unsupported.append(pname)
            else:
                g.missing.append(pname)
        elif spec.get("type") == "boolean":
            flag = _as_bool(value)
            if flag is None:
                g.missing.append(pname)
            else:
                _set(g, pname, flag, "cloud")
        elif spec.get("type") == "number":
            try:
                _set(g, pname, float(value), "cloud")
            except (TypeError, ValueError):
                g.missing.append(pname)
        elif kind.startswith("entity:"):
            hit = s.match_entity(str(value), entity_names(kind[7:], snap))
            if hit is None:
                g.missing.append(pname)
            else:
                _set(g, pname, hit, "cloud")
        elif kind == "places":
            _ground_places(g, pname, split_places(value), cabin, how="cloud")
        elif kind == "place":
            place = _place_by_name(str(value), cabin)
            if place is None:
                g.missing.append(pname)
                heard = str(value).strip().rstrip(".!?")
                if heard:
                    g.unresolved[pname] = heard
            else:
                _set(g, pname, place, "cloud")
        else:
            _set(g, pname, str(value).strip(), "cloud")
    for pname, spec in api.params.items():
        if pname in g.args or pname in g.missing or pname in g.unsupported:
            continue
        if "default" in spec:
            _set(g, pname, spec["default"], "default")
    return _finish(api, g)


_CLARIFY = {
    "name": "Who should I call? I don't see that name in your contacts.",
    "recipient": "Who should I send it to? I don't see that name in your contacts.",
    "text": "What should the message say?",
    "destination": "Where would you like to go?",
    "query": "What would you like to play?",
    "category": "What kind of place are you looking for?",
    "mode": "Which setting would you like?",
    "locked": "Lock or unlock?",
    "source": "Which source: radio, Bluetooth, USB or streaming?",
    "signal": "What would you like to know about the car?",
    "zone": "Which side do you mean?",
}


def apply_focus_zone(cabin: CabinEnv, text: str, outcome: RouteOutcome) -> RouteOutcome:
    """Keep a follow-up on the zone already in play.

    A cloud call that omits the zone is stored as the schema default (the
    driver). If this utterance names a zone, or continues one from the last
    cabin command or the recent dialogue, that zone replaces the default.
    """
    if outcome.kind != "vehicle" or not outcome.calls:
        return outcome
    named, how = s.parse_zone(
        text, ("driver", "passenger", "all", "front", "rear", "rear_left", "rear_right"), None,
    )
    use_named = how == "parser" and named is not None
    use_past = not use_named and s.continues_request(text)
    calls: list[VehicleCall] = []
    changed = False
    for call in outcome.calls:
        api = cabin.devices[call.device].apis[call.api]
        spec = api.params.get("zone")
        if not spec:
            calls.append(call)
            continue
        current = call.args.get("zone")
        source = call.grounding.get("zone")
        default = spec.get("default")
        if source == "cloud" and not use_named and current != default and default in spec["enum"]:
            # The model carried a side the driver gave for another control ("the seat, driver
            # side only") onto this one; "set it to one" then changed only half the cabin.
            said, about = cabin.dialog_zone(spec["enum"])
            if current == said and about and call.device not in about:
                args = dict(call.args)
                args["zone"] = default
                grounding = dict(call.grounding)
                grounding["zone"] = "default"
                calls.append(VehicleCall(call.device, call.api, args, grounding, call.score))
                changed = True
                continue
        if not use_named and not use_past:
            calls.append(call)
            continue
        if source == "parser" or (source in {"cloud", "enum", "focus", "dialog"} and current != default):
            calls.append(call)
            continue
        replacement = None
        how_set = None
        if use_named and named in spec["enum"]:
            replacement, how_set = named, "parser"
        elif use_past:
            replacement = (cabin.focused_zone(call.api, spec["enum"])
                           or cabin.zone_from_dialog(spec["enum"], call.device))
            how_set = "focus" if cabin.focused_zone(call.api, spec["enum"]) else "dialog"
        if not replacement or replacement == current:
            calls.append(call)
            continue
        args = dict(call.args)
        args["zone"] = replacement
        grounding = dict(call.grounding)
        grounding["zone"] = how_set or "focus"
        calls.append(VehicleCall(call.device, call.api, args, grounding, call.score))
        changed = True
    if not changed:
        return outcome
    return RouteOutcome("vehicle", calls, outcome.speak, dict(outcome.trace))


def clarify_speak(
    api: Api, missing: list[str], unresolved: Mapping[str, str] | None = None,
) -> str:
    tried = unresolved or {}
    for name in missing:
        heard = str(tried.get(name) or "").strip().rstrip(".!?")
        # A place was named and search missed. Asking "where?" again sounds like we heard nothing.
        if name == "destination" and heard:
            return f"I couldn't find {heard}."
        if name == "via" and heard:
            return f"I couldn't find {heard} to drive through."
        if name in _CLARIFY:
            return _CLARIFY[name]
    return "Could you say that again with the setting you want?"


def decline_speak(api: Api, unsupported: list[str]) -> str:
    if "zone" in unsupported:
        return "That isn't available for the rear seats in this car."
    return "That isn't available in this car."
