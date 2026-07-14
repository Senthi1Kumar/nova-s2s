"""Current-weather lookup for a named place, exposed as a ``NovaTool``.

Uses Open-Meteo (https://open-meteo.com/) — free, no API key required: its
geocoding endpoint resolves a place-name to lat/lon, then the forecast
endpoint returns current conditions for that point. "Place not found" and any
network/HTTP failure degrade to a clear result dict rather than raising, so a
bad place-name never crashes the voice loop.
"""
from __future__ import annotations

from typing import Any

import httpx

from nova.tools.base import NovaTool

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes (subset used by Open-Meteo's current_weather).
_WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "slight snow fall",
    73: "moderate snow fall",
    75: "heavy snow fall",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _describe(code: int) -> str:
    return _WEATHER_CODES.get(code, "unknown conditions")


# Common spoken aliases → Open-Meteo-friendly place names (demo locales).
_PLACE_ALIASES = {
    "bangalore": "Bengaluru",
    "bengalooru": "Bengaluru",
    "truchi": "Tiruchirappalli",
    "trichy": "Tiruchirappalli",
}


def _normalize_place(place: str) -> str:
    key = " ".join(place.strip().lower().split())
    return _PLACE_ALIASES.get(key, place.strip())


class WeatherTool(NovaTool):
    name = "get_weather"
    description = (
        "Get the current outdoor temperature, forecast, and weather conditions "
        "for a named place (city, town, or landmark). Use for outside/outdoors "
        "weather — not cabin or HVAC climate controls."
    )
    parameters = {
        "type": "object",
        "properties": {
            "place": {
                "type": "string",
                "description": "Place name, e.g. 'Bangalore', 'Bengaluru', or 'Paris, France'.",
            },
        },
        "required": ["place"],
    }

    def __init__(self, timeout: float = 5.0):
        self._timeout = timeout

    def _geocode(self, place: str) -> dict[str, Any] | None:
        resp = httpx.get(
            GEOCODING_URL,
            params={"name": place, "count": 1, "language": "en"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return results[0] if results else None

    def _current_conditions(self, lat: float, lon: float) -> dict[str, Any]:
        resp = httpx.get(
            FORECAST_URL,
            params={"latitude": lat, "longitude": lon, "current_weather": "true"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()["current_weather"]

    def execute(self, place: str) -> dict[str, Any]:
        place = _normalize_place(place)
        try:
            location = self._geocode(place)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return {"status": "error", "reason": f"weather lookup failed: {exc}"}

        if location is None:
            return {"status": "not_found", "place": place}

        try:
            current = self._current_conditions(location["latitude"], location["longitude"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return {"status": "error", "reason": f"weather lookup failed: {exc}"}

        return {
            "status": "success",
            "place": location.get("name", place),
            "country": location.get("country", ""),
            "temp_c": current["temperature"],
            "condition": _describe(current["weathercode"]),
            "speak": (
                f"{location.get('name', place)} is "
                f"{_describe(current['weathercode'])}, "
                f"{current['temperature']} degrees Celsius."
            ),
        }
