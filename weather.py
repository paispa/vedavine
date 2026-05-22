"""weather.py - fetch a short local-weather summary from the US NWS API.

Used to ground VedaVine's assessment in current disease/pest pressure
(humidity + rain favor mildew, etc.). US-only (api.weather.gov). This is the
ONE outbound call VedaVine makes besides localhost Ollama — it sends only the
vineyard's lat/lon, never the photo. Everything fails soft to "" so a weather
outage never breaks /analyze.

NWS requires a descriptive User-Agent or it returns 403.
"""

from __future__ import annotations

import requests

NWS_BASE = "https://api.weather.gov"
ZIPPO_BASE = "https://api.zippopotam.us/us"


def geocode_zip(zipcode: str, user_agent: str = "VedaVine/1.0", timeout: int = 10):
    """US ZIP -> (lat, lon), or None on any failure. Uses free zippopotam.us."""
    try:
        r = requests.get(
            f"{ZIPPO_BASE}/{zipcode.strip()}",
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
        r.raise_for_status()
        place = r.json()["places"][0]
        return float(place["latitude"]), float(place["longitude"])
    except Exception:  # noqa: BLE001 — best-effort; caller falls back
        return None


def _periods(lat: float, lon: float, user_agent: str, timeout: int) -> list[dict]:
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    pts = requests.get(f"{NWS_BASE}/points/{lat},{lon}", headers=headers, timeout=timeout)
    pts.raise_for_status()
    forecast_url = pts.json()["properties"]["forecast"]
    fc = requests.get(forecast_url, headers=headers, timeout=timeout)
    fc.raise_for_status()
    return fc.json()["properties"]["periods"]


def weather_context(
    lat: float,
    lon: float,
    user_agent: str = "VedaVine/1.0",
    n_periods: int = 2,
    timeout: int = 15,
) -> str:
    """One line per forecast period (name, conditions, temp, humidity), or ""."""
    try:
        periods = _periods(lat, lon, user_agent, timeout)
    except Exception:  # noqa: BLE001 — weather is best-effort; never raise
        return ""
    lines = []
    for p in periods[:n_periods]:
        rh = p.get("relativeHumidity") or {}
        hum = f", humidity ~{rh['value']}%" if isinstance(rh, dict) and rh.get("value") is not None else ""
        wind = f", wind {p['windSpeed']}" if p.get("windSpeed") else ""
        lines.append(
            f"{p.get('name', '')}: {p.get('shortForecast', '')}, "
            f"{p.get('temperature', '')}°{p.get('temperatureUnit', 'F')}{hum}{wind}."
        )
    return "\n".join(lines)
